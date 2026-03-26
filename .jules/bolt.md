## 2024-05-20 - [Optimize greedy set cover algorithm]
**Learning:** In the greedy algorithm for selecting tests, computing `marginal_set = test.branches_covered & uncovered` on every iteration is O(N*M) and very slow.
**Action:** We can pre-compute the valid branches for each candidate and keep them in a mutable list. When a new test is picked, we only subtract the newly covered branches from each candidate's branch set (`candidate[1] -= new_covered`), doing operations in place and dynamically tracking remaining marginal coverage. This dramatically speeds up execution.
