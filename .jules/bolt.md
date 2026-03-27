## 2024-05-20 - [Optimize greedy set cover algorithm]
**Learning:** In the greedy algorithm for selecting tests, computing `marginal_set = test.branches_covered & uncovered` on every iteration is O(N*M) and very slow.
**Action:** We can pre-compute the valid branches for each candidate and keep them in a mutable list. When a new test is picked, we only subtract the newly covered branches from each candidate's branch set (`candidate[1] -= new_covered`), doing operations in place and dynamically tracking remaining marginal coverage. This dramatically speeds up execution.

## 2024-05-20 - [Optimize equivalent tests calculation]
**Learning:** Hashing `frozenset` strings of coverage branches thousand of times (e.g. `hashlib.sha256(",".join(sorted(branches)).encode()).hexdigest()`) takes a substantial amount of time.
**Action:** `frozenset` objects are natively hashable in Python, so they can be used directly as dictionary keys to group tests. Only calculate the expensive string hash for groups that have >1 test, as the hash is only needed for the output report to give each equivalent group a unique identifier.
