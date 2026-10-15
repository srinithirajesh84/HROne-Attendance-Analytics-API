# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | | | | |

Also note anything you looked at and decided was **not** a defect, and why.
# REVIEW.md

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `explain_endpoint` — `attendance_list` branch | MongoDB explain results may contain BSON values, such as `Timestamp`, that cannot be serialized directly into a JSON response. | Call `/admin/explain/attendance_list` with valid query parameters. The endpoint may return HTTP 500 due to a serialization error. | Added `make_json_safe()` to recursively convert BSON values into JSON-compatible values before returning the response. |

## Not a defect

| What I checked | Why I did not consider it a defect |
|---|---|
| Duplicate punch-in requests | The unique compound index on `emp_code` and `date` prevents duplicate attendance records for the same employee and date. |
| Leaderboard ties | MongoDB `$rank` assigns equal ranks to employees with equal values. Filtering by rank preserves ties at the requested limit. |
| Employees without attendance logs | The department summary starts from employee documents and looks up attendance records, so employees without logs are included in the headcount. |
