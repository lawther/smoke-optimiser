## 2024-05-20 - [Optimize greedy set cover algorithm]
**Learning:** In the greedy algorithm for selecting tests, computing `marginal_set = test.branches_covered & uncovered` on every iteration is O(N*M) and very slow.
**Action:** We can pre-compute the valid branches for each candidate and keep them in a mutable list. When a new test is picked, we only subtract the newly covered branches from each candidate's branch set (`candidate[1] -= new_covered`), doing operations in place and dynamically tracking remaining marginal coverage. This dramatically speeds up execution.

## 2024-05-20 - [Optimize equivalent group calculation]
**Learning:** Hashing `frozenset` instances natively in Python is vastly faster than computing stable string hashes (like SHA256) of stringified set contents. Generating stable hashes for every test upfront in large suites is extremely costly.
**Action:** When finding duplicates or grouping sets of data (like coverage sets), always use the native C-implemented `frozenset` hashing and equality for mapping logic (`branch_set_to_tests = defaultdict(list)`). Only calculate stringified representations or expensive deterministic hashes when absolutely necessary for the actual matching groups (when `len(group) > 1`).
