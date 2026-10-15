# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

# DECISIONS.md

1. Indexes-I created a unique index on `emp_code` to prevent duplicate employees and a unique compound index on `emp_code` and `date` to prevent duplicate attendance records. Department indexes support employee filtering and department reporting. Attendance indexes support date-based listings, status filtering, and late-minute queries. I did not add a separate email index because the current API does not search employees by email.

2. Punch-in race- If two identical punch-in requests arrive together, both may initially find the employee. However, the unique attendance index allows only one record for the same employee and date. The other insert raises `DuplicateKeyError` and returns HTTP 409.

3. Ties-The leaderboard uses MongoDB `$rank` and filters ranks up to the requested limit. Employees tied at the cutoff receive the same rank, so all employees with that rank are included.

4. Headcount-The department summary starts from employee documents, groups them by department, and then looks up attendance records. Employees without attendance logs are still counted, and missing attendance statistics default to zero.

5. Scaling-For 100 times more data, I would measure query performance, optimise aggregation pipelines and indexes, and consider precomputed daily summaries to reduce repeated calculations.
