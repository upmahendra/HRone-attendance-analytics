# DECISIONS.md

1. **Indexes.** I create a unique index on `employees.emp_code` because the business key must be unique and concurrency-safe. I also add a compound unique index on `attendance_logs` for `(emp_code, date)` so a single employee can never have two records for the same attendance day, even under race conditions. I add supporting indexes on `department`, `joined_on`, `status`, and the date/emp filters to keep list and analytics queries efficient. I did not add a broad text index because the API never searches by arbitrary text and it would be unnecessary overhead.

2. **Punch-in race.** If two identical punch-in requests hit at the same time, both will check whether the employee exists and then try to insert a record for the same `(emp_code, date)`. The unique compound index guarantees only one insert succeeds. The second request hits `DuplicateKeyError`, which is converted into a 409 `already punched in for this date`, so the API preserves correctness instead of creating a duplicate.

3. **Ties.** The leaderboard ranks by total late minutes descending, then `emp_code` ascending. If two employees are tied at the cutoff, they receive the same competition rank, and every employee with that rank is returned as long as their rank is `<= limit`. That matches the contract and prevents using array position as the rank.

4. **Headcount.** The department summary starts from the full employee roster filtered by `joined_on <= month_end`, then adds the department totals from logs. That means employees with zero records are still included in `headcount`, and the month summary counts them even when they have no attendance rows.

5. **One thing to change for 100x the data.** I would move the heavy month/department analytics to MongoDB aggregations instead of pulling full collections into Python loops. That would reduce network and memory overhead, and it would let the database do the rolling sums, grouping, and date densification with better index support.
