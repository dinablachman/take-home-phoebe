## Design Decisions / Tradeoffs

### 1. Handling time:

The biggest challenge which came up for me while implementing the escalation logic was simulating the 10 minute wait for each shift fanout. The example_test.py file shows how to use freezegun to mock moving time forward, but I also use asyncio to schedule tasks, which relies on a monotonic clock. If freezegun freezes the clock in test functions, the event loop will hang forever. However, the logic is dependent on each task's timing being accurate w.r.t the other (multiple fanouts can run concurrently).

In create_app() I set separate functions now_fn and sleep_fn on app.state, with now_fn() using real current time and sleep_fn(seconds) actually sleeping. I use real time in api.py to model the actual wait times as if it was called in production, but in my test_shift_fanout.py, I swap sleep_fn() with a fake sleep that blocks until I manually advance frozen time with freezegun. In tests, asyncio scheduling still works with real_asyncio=True, so background tasks still run properly, but datetime.now() returns frozen time.

I chose to implement time this way because other methods were less efficient and didn't align exactly with the constraints. For example, I first implemented a polling loop which started the background task with asyncio, repeatedly checked the clock and shift state until 10 minutes passed, then initiated escalation. This required a sleep after each check. If the poll interval is very small to make tests fast, then it must check repeatedly, which burns CPU. As I was given flexibility in this part of the implementation, I wanted to demonstrate how the timing logic might work in practice (with the 10 minute timedelta), as well as how it can be modified for testing while keeping underlying logic the same.

### 2. Data model design:

I kept the model design and database fairly simple and did not create additional tables/collections for state tracking. I implemented the Caregiver and Shift classes with additional fields to represent the relationship between the two (time a fanout starts / when a shift is claimed, and caregivers who claim / decline a shift). In a more concrete implementation, I would have enforced a primary key constraint on both ID fields, and foreign key constraint between Caregiver.id and Shift.claimed_by. I would have also added a type definition for roles (LPN, TPN, etc) instead of string matching.

I added the atomic operation claim_shift_if_unclaimed() to database.py which returns True if a claim succeeded and False otherwise. This is not cross-process safe but only atomic within one Python process. Given the scope of the project and my results after testing atomicity, I believe this is sufficient to handle 'race conditions' where two caregivers try to claim a shift at once. I would use distributed locking in production with multiple processes.

### 3. Idempotency:

In api.py, I check idemopotency before any 'await calls' and set fanout_started_at before any async operations so that the change persists to the DB. For testing, I return the status 'already_fanout' when a duplicate fanout request is made.

### 4. Outreach / response logic:

I chose to differentiate between DECLINE/UNKNOWN responses for SMS and phone calls. Only a DECLINE would exclude a caregiver from being contacted again during phone call escalation, while an UNKNOWN would not remove them from the escalation list. I did this because a caregiver who has already declined a shift over text would likely refuse it again over the phone, but a caregiver who provided an unknown response may not have declined (it could be a parsing error, etc) and should not necessarily be excluded from being able to claim the shift. However, only an ACCEPT allows a caregiver to claim a shift.

When escalating after sleep has finished, the shift is reread from the DB to get the latest state, in case it has been claimed or declined caregivers changed.

I implemented caregiver lookup in a simplistic way that would be inefficient with a very large DB. I use next() to find caregivers by phone number, which runs in O(n) time. I would use indexed lookup for a production DB with many caregivers.
Additionally, I generate a full list of matching caregivers before beginning every fanout. I chose to do this to simulate the behavior of a text/phone blast being sent simultaneously. If parsing through matching caregivers is very lengthy, it might be more efficient to reach out in batches as matches are found, but it would not match the intended behavior of the spec.

### 5. Testing / Patching:

I created a custom FreezegunSleeper class which works with freezegun to control time in the testing script, independently of my api.py implementation. I also used AsyncMock to mock async functions api.send_sms and api.place_phone_call at the module level. This is to eliminate the asyncio sleep that simulates API call time. This does not affect api.py because it imports from app.notifier (it only makes testing more efficient).

My test script defines test data that matches the structure from sample_data.json but with simpler IDs for readability while testing.

## LLM Tool Use:

I used Cursor and GPT-5.2 for debugging and writing test cases.

I first drew out a step-by-step implementation plan and data diagram. I used GPT for concept understanding: it explained how timing of concurrent background tasks works in a Python microtransaction. Cursor was also helpful for understanding the initial directory when in 'ask' mode because it could grep the entire codebase and answer high-level questions.

I also found Cursor helpful to debug my issues with asyncio / freezegun by diagnosing logs. I used LLMs most for writing test cases and debugging individual issues rather than implementing broad logic, because the task is open-ended and both Cursor and GPT tended to overengineer and make design decisions outside the scope of a certain prompt. Cursor worked well for me when I prompted it with a modular task at a time and reviewed diffs one by one. 