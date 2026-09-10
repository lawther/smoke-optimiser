# Changelog

## Unreleased

### ⚠ BREAKING CHANGES

* the CLI now has two subcommands rather than one bare command. The existing coverage-per-second
  path moves from `smoke-optimiser <options>` to `smoke-optimiser smoke <options>`, with every
  option unchanged, and the new change-based selection is `smoke-optimiser downwind`. Scripts,
  precommit hooks and CI steps invoking the bare command must add `smoke`. The canonical repro
  command that `--output-json` records names the subcommand too.
* profiling data files now carry a required `schema_version` field. A profile written by an
  earlier version has no such field and will be rejected on load with a message naming the
  expected and found versions -- re-run the profiling phase to regenerate it.
* profiling data files are now at `schema_version` 2, which adds a required `scope` field
  recording the coverage targets and test paths the profiling run measured. A version 1
  profile is rejected on load -- re-run the profiling phase to regenerate it.

## [0.3.0](https://github.com/lawther/smoke-optimiser/compare/v0.2.0...v0.3.0) (2026-09-10)


### ⚠ BREAKING CHANGES

* **downwind:** .downwind.json is version 2. NON_PYTHON_FILE is gone and UNATTRIBUTED_READ, ENVIRONMENT_FILE and READ_ERRORS take its place, so a version 1 file names a reason this build has no word for and is refused by version rather than failing enum validation with nothing actionable to say.
* **profiler:** PROFILE_SCHEMA_VERSION is 3. A profile is a cache one instrumented run rebuilds, so there is no migration -- an older profile is reported as a mismatch and the user reprofiles.
* **downwind:** the CLI now has two subcommands rather than one bare command. The coverage-per-second path moves from `smoke-optimiser <options>` to `smoke-optimiser smoke <options>`, with every option unchanged, and the new change-based selection is `smoke-optimiser downwind`. Scripts, precommit hooks and CI steps invoking the bare command must add `smoke`. The canonical repro command recorded in the smoke suite file names the subcommand too.
* **profiler:** profiling data files are now at schema_version 2, which adds a required scope field. A version 1 profile is rejected on load -- re-run the profiling phase to regenerate it.
* profiles written before this change fail validation and must be regenerated.

### Features

* add version-controlled git hooks and auto-install on precommit ([05e3118](https://github.com/lawther/smoke-optimiser/commit/05e31186345f213b1773b6549b30161299aefedc))
* capture the import graph, and support xdist during profiling ([2a7beb6](https://github.com/lawther/smoke-optimiser/commit/2a7beb68bb2a525a4b101745dc778d5c844a2f8a))
* **downwind:** answer for a changed data file instead of running everything ([ead3a12](https://github.com/lawther/smoke-optimiser/commit/ead3a125226e1faa06824c742ab30c95f9b153a0))
* **downwind:** answer from the maps, or refuse and name the blind spot ([fbecdf9](https://github.com/lawther/smoke-optimiser/commit/fbecdf993469be3d994b95f2999eb48d0f77c501))
* **downwind:** build the file-to-tests and file-to-dependents maps ([e2b6820](https://github.com/lawther/smoke-optimiser/commit/e2b682073433550574cc904e8528f16fe1bf2b97))
* **downwind:** collect the changed-file set from git, staged/unstaged/untracked ([a9b7d80](https://github.com/lawther/smoke-optimiser/commit/a9b7d807b1e5cbcd67c788add2767a5b41bde366))
* **downwind:** expose the graph's blind spots on DownwindMaps ([0b6fcca](https://github.com/lawther/smoke-optimiser/commit/0b6fcca2561972cce08e970e815e1473d7bdd286))
* **downwind:** give the downwind selection its own file schema and pytest flag ([fd5be2a](https://github.com/lawther/smoke-optimiser/commit/fd5be2ab3a354a386c192f2ae11986222d480296))
* **downwind:** map each file to the tests defined in it ([2036c76](https://github.com/lawther/smoke-optimiser/commit/2036c768e8d722ec3f4709e3bbf2ed037186dd05))
* **downwind:** run the full-suite fallback instrumented, so it repairs the map ([39fcb3f](https://github.com/lawther/smoke-optimiser/commit/39fcb3f3cacc657997dea0478838e74962fb70d2))
* **downwind:** wire up the downwind command, from git diff to pytest invocation ([b64e553](https://github.com/lawther/smoke-optimiser/commit/b64e553aab24ea51cb9ad388f6f11fda87065b7b))
* **plugin:** refuse --smoke and --downwind together ([74fd5e4](https://github.com/lawther/smoke-optimiser/commit/74fd5e4210450f90ed4a25008368a609234b7dd8))
* **profiler:** record the scope a profile was captured under ([5ee6239](https://github.com/lawther/smoke-optimiser/commit/5ee623902bd330cddcf92e7d54daacaaf05dd959))
* **profiler:** record which files each test reads and which directories it lists ([94e9fee](https://github.com/lawther/smoke-optimiser/commit/94e9feed1c36731b8e0226ed76f2abd895833f70))
* **profiler:** suggest the recorded command on a schema mismatch ([fd7f08d](https://github.com/lawther/smoke-optimiser/commit/fd7f08dd8eb9faadb0a4b7ca3fd493e0249ac641))
* **profiler:** version the profiling data file schema ([7a16493](https://github.com/lawther/smoke-optimiser/commit/7a164937c4ea1be477a7f11226a6693c4b6b6f9f))
* read per-test coverage from coverage.py's SQLite database ([f2dfaf0](https://github.com/lawther/smoke-optimiser/commit/f2dfaf02839a9791c91656891a2281c02e12bc38))
* record per-test file coverage so branchless files are visible ([95e8f36](https://github.com/lawther/smoke-optimiser/commit/95e8f36e43b3b3754c3c6a0c22925abd9b3548cd))


### Bug Fixes

* **config:** stop CLI boolean defaults clobbering pyproject.toml settings ([df38120](https://github.com/lawther/smoke-optimiser/commit/df381200f084398bc25c07c4e05755e5e66e6216))
* **coverage:** explain and offer a fix for non-Python measured files ([6b75edf](https://github.com/lawther/smoke-optimiser/commit/6b75edf4886ebe7154b1548e1abc870617926790))
* **coverage:** treat non-Python measured files as branchless, not fatal ([5c74d7f](https://github.com/lawther/smoke-optimiser/commit/5c74d7f6a48caecd0e4332d5a30877c629ddb200))
* **downwind:** answer for a change that reaches tests only through a conftest ([b635bda](https://github.com/lawther/smoke-optimiser/commit/b635bdaa6c78cff4893d56b83a7559c3a1a4df3a))
* **downwind:** answer for a Python file the run measured as unloaded ([5406eae](https://github.com/lawther/smoke-optimiser/commit/5406eaec1c249f1399ccdee89ea8346f4f242fd4))
* **downwind:** count the blind spots, not every file that happened to change ([fe5f85b](https://github.com/lawther/smoke-optimiser/commit/fe5f85b2f418639eae4dcf0f6643100bbfdf276f))
* **downwind:** never treat the tool's own artefacts as a changed file ([4fc318b](https://github.com/lawther/smoke-optimiser/commit/4fc318bbc62b615ca8d3d398590b15bdfc735853))
* **downwind:** stop the kept copy of an unreadable profile becoming the next blind spot ([d9fa0cb](https://github.com/lawther/smoke-optimiser/commit/d9fa0cbce5abf7c87f3ac76410ea240e49fb6713))
* only treat --cov itself as the user choosing what to measure ([8d3bfb9](https://github.com/lawther/smoke-optimiser/commit/8d3bfb9566553729e5fa50b91fce12ecf31d8235))
* **profiler:** fail the run when pytest leaves the suite only partly profiled ([9949bb1](https://github.com/lawther/smoke-optimiser/commit/9949bb153a0ac639f23082a6e247cf1497c1a7fd))
* **profiler:** report an unrecognised pytest exit code instead of raising ([b4181f4](https://github.com/lawther/smoke-optimiser/commit/b4181f4e576a886a599c1e64a3c7fcdf419fb1f5))
* **profiler:** save the profile on every run, not only --profile-only ([df84630](https://github.com/lawther/smoke-optimiser/commit/df8463010ce1f1e68a328b9ed7774f735257fea0))
* **profiler:** stop a non-package directory under a coverage root expiring the profile forever ([847165b](https://github.com/lawther/smoke-optimiser/commit/847165b771538b602b286ec784e646094fe77dc1))
* **profiler:** stop blaming a stale database for every unattributable context ([d53b171](https://github.com/lawther/smoke-optimiser/commit/d53b1711278643203cc7a9c927c326160919f0a0))
* **profiler:** stop running the whole suite to check for pytest-randomly ([a91e609](https://github.com/lawther/smoke-optimiser/commit/a91e609bc2ce7723368725c5b6a2182b8d22b193))
* **profiler:** treat a missing artefact as an error, not as an empty one ([0210cbe](https://github.com/lawther/smoke-optimiser/commit/0210cbe5ee73d0a196b223cddd92b32b8602e601))
* **profiler:** write the profile atomically, so a killed run keeps the old one ([dea952c](https://github.com/lawther/smoke-optimiser/commit/dea952cde9d3abd40fa9b27d677765e096d64636))
* record failed setup/teardown phases so erroring fixtures don't abort profiling ([d5ab406](https://github.com/lawther/smoke-optimiser/commit/d5ab406cbc5214e1a970b548baea6edc753d2106))
* report an invalid profile as a CLI error instead of a traceback ([5634ec1](https://github.com/lawther/smoke-optimiser/commit/5634ec166e600c8677f71e0af196c5959b12d6d5))
* **reports:** validate BlindSpotModel and DownwindSuiteFile invariants ([ae8b24b](https://github.com/lawther/smoke-optimiser/commit/ae8b24be8ef7b6cb2ac4336c8eca557974cb804a))
* **tests:** strip ANSI codes before asserting on CLI help text ([ff1b973](https://github.com/lawther/smoke-optimiser/commit/ff1b9732906c08a1e8f7946c66640e3c9593accf))
* validate external data with Pydantic to satisfy ML400 lint ([e33671b](https://github.com/lawther/smoke-optimiser/commit/e33671b65018f59214da4b785c573910dca1aa68))


### Documentation

* describe the file-level data in the coverage_db module docstring ([7e114c8](https://github.com/lawther/smoke-optimiser/commit/7e114c8453ab6cc244d64886934e470b1b6a39c4))
* document unattributable_branches in the profile format ([42cc2e2](https://github.com/lawther/smoke-optimiser/commit/42cc2e23ff801abe90144c5c24233d33b9ac20ea))
* **downwind:** fix inaccurate blind-spot reason grouping in BlindSpotReason docstring ([d7e8fcc](https://github.com/lawther/smoke-optimiser/commit/d7e8fcca219ef0ef30a9db3edc69789bf8767a93))
* **downwind:** state the conftest directory scope in the selection file's claim ([a796f99](https://github.com/lawther/smoke-optimiser/commit/a796f99634d4db1f2ef0074906bf1de0a7db7023))
* **prd:** document allow_parallel_durations in the configuration table ([933cc50](https://github.com/lawther/smoke-optimiser/commit/933cc50e247fa442230d81c7c471c5dae8297b02))
* **readme:** restructure around the smoke/downwind split ([122e1c2](https://github.com/lawther/smoke-optimiser/commit/122e1c2a2994b355a5652b3028b07f4557fa7568))
* require issues to be rewritten rather than appended to ([6ca6328](https://github.com/lawther/smoke-optimiser/commit/6ca6328116886d445d552f7ff83b03b71c902d38))
* **tests:** say what the fixture's .gitignore is actually for ([ba7a42b](https://github.com/lawther/smoke-optimiser/commit/ba7a42b461380bd15a91eccf3490218ab5b5e5b0))

## [0.2.0](https://github.com/lawther/smoke-optimiser/compare/v0.1.0...v0.2.0) (2026-04-30)


### Features

* upgrade release-please-action to v5 ([1506f37](https://github.com/lawther/smoke-optimiser/commit/1506f373e6d97aba3d5a377a1c50a36e49193579))

## 0.1.0 (2026-04-28)


### Features

* add --iterations argument to average test durations across multiple runs ([e85b02c](https://github.com/lawther/smoke-optimiser/commit/e85b02ce94a195bf8510a6d64e889da544fe6826))
* add --src top-level argument for coverage target with conflict validation ([e6a1373](https://github.com/lawther/smoke-optimiser/commit/e6a13737aed3e1f96c6149036aed4ae3044e19dd))
* add CLI argument parsing and config merging ([6b77dde](https://github.com/lawther/smoke-optimiser/commit/6b77dde50b42ffd71c5ed8a544f1eb6af439defa))
* add config model with pyproject.toml loading ([e7f85a7](https://github.com/lawther/smoke-optimiser/commit/e7f85a7fa3eb1e629dba839770cbf1a76044abec))
* add coverage JSON parser with streaming support ([1cad88f](https://github.com/lawther/smoke-optimiser/commit/1cad88f83c2d107ce2da481f2643548f254b45a3))
* add full suite coverage metric to summary reports ([db6f218](https://github.com/lawther/smoke-optimiser/commit/db6f218420b748206ad377324f9a0af5d053552d))
* add greedy set-cover optimiser ([0015142](https://github.com/lawther/smoke-optimiser/commit/00151429c2ef326384286c2dc3777a11465016c0))
* add heuristic coverage target discovery with warning ([9ada322](https://github.com/lawther/smoke-optimiser/commit/9ada322039e51a8218e6020a507a1bde017cad51))
* Add Justfile for development task automation ([6aa80bc](https://github.com/lawther/smoke-optimiser/commit/6aa80bc3d41f85f9aa0f74c2e8f7ab333a668666))
* add machine environment capture ([b8ef0f9](https://github.com/lawther/smoke-optimiser/commit/b8ef0f92e58eecd0d52949a50a8e5343136919b7))
* add optimiser data models ([4f6bbf4](https://github.com/lawther/smoke-optimiser/commit/4f6bbf4ae1a4a21dfe2fffac1496296eb445cc3b))
* add optimiser filters (include/exclude/failing) ([5b8c049](https://github.com/lawther/smoke-optimiser/commit/5b8c0492686a08703caa7d73684cc2a7c7369f7d))
* add plugin summary header showing smoke suite metadata ([7eabb0c](https://github.com/lawther/smoke-optimiser/commit/7eabb0cd806b58db89e40b6bec2619bfce8ea4d4))
* add profiler data models ([0ece94c](https://github.com/lawther/smoke-optimiser/commit/0ece94cf4d93f7d39217d35d94522f030b902000))
* add profiler runner (pytest + coverage orchestration) ([dd9b6ae](https://github.com/lawther/smoke-optimiser/commit/dd9b6ae3396afc471809dd52cfa7f097b1edb2d6))
* add pytest plugin option registration and smoke suite loading ([44eee93](https://github.com/lawther/smoke-optimiser/commit/44eee93464936724cf79e2e2f19b941f82b6b0f6))
* add report generation (JSON and human-readable) ([4f761b6](https://github.com/lawther/smoke-optimiser/commit/4f761b6ab4a690f20f616ef3d61b4a691a2de2a5))
* add test collection filtering for --smoke ([f1ab66e](https://github.com/lawther/smoke-optimiser/commit/f1ab66edfdec290db8b83d6d4007bd610ce6de55))
* allow matching include/exclude filters by test function name ([b85bcd0](https://github.com/lawther/smoke-optimiser/commit/b85bcd066c0d6936c67a7ef28f1d1c36cc9d6ce5))
* enable direct installation of `smoke-optimiser`, update README instructions, and add MIT license. ([5c9c616](https://github.com/lawther/smoke-optimiser/commit/5c9c616a7133db88c7c43e8d4984ff7a1412f9cf))
* support comma-separated patterns in include and exclude filters ([985f878](https://github.com/lawther/smoke-optimiser/commit/985f87886beb16fbea560176efcdc2c5c454d16e))
* support file-level matching in include/exclude filters ([bb8f8db](https://github.com/lawther/smoke-optimiser/commit/bb8f8db337241952d085ccad221262357de24441))
* warn user about unmatched include/exclude patterns during optimization ([41cabaa](https://github.com/lawther/smoke-optimiser/commit/41cabaa457526812a15f31275512637b895c2700))
* wire CLI to profiler and optimiser phases ([d2a3534](https://github.com/lawther/smoke-optimiser/commit/d2a35345f87998db077e9a2aa40ab2a409b45433))


### Bug Fixes

* arbitrary file overwrite (Symlink Attack) ([7e97075](https://github.com/lawther/smoke-optimiser/commit/7e97075139f4ddf64493c73a3e2face57c013c9a))
* capture heuristically discovered coverage target in repro command ([401f5d3](https://github.com/lawther/smoke-optimiser/commit/401f5d33a276e97d2d05289330b3dd434d749452))
* Enhance CLI message consistency and add pluralisation ([20a7c56](https://github.com/lawther/smoke-optimiser/commit/20a7c56ed7a711e7cdc4ed1fc7519d4fe010d6be))
* Fix B110 exception swallowing in config parser ([aa182f9](https://github.com/lawther/smoke-optimiser/commit/aa182f9e3d70e4e396914114423a0b7b6074a512))
* insecure relative path generation in hook output ([ec54988](https://github.com/lawther/smoke-optimiser/commit/ec54988462fea499c9ccb79f041bdbc24c7269ae))
* Predictable Temporary File Names in Project Root ([9ffdf7f](https://github.com/lawther/smoke-optimiser/commit/9ffdf7fc9866a8eb02b52bf4cdcd9e94a23be738))
* prevent information disclosure on invalid profile data ([6d3dfac](https://github.com/lawther/smoke-optimiser/commit/6d3dface5ed0267593c668cf7af8297293a66717))
* profiler hook loading and streaming parser robustness ([f8e7637](https://github.com/lawther/smoke-optimiser/commit/f8e7637cad0ec6540360b43305bd06361c46fb4c))
* refactor Justfile for reliable error reporting and 4-space indentation ([4a588ba](https://github.com/lawther/smoke-optimiser/commit/4a588bafd273f784219d16a39ed23566cd9cc951))
* refactor Justfile recipes for reliable error reporting and add linting ([8b96301](https://github.com/lawther/smoke-optimiser/commit/8b96301ef22b0b0b8420cb257ee500c87c7dda8f))
* refactor precommit recipe for reliable error reporting ([67f2918](https://github.com/lawther/smoke-optimiser/commit/67f29189b9045ea0dc93a20b7e30e43d654a5833))
* resolve conflicting coverage dynamic contexts and promote warnings to errors ([17a16dd](https://github.com/lawther/smoke-optimiser/commit/17a16ddd7c7b5d152ce92cdaf19f3e6124f545c7))
* resolve partial executable paths using `shutil.which` ([d3619b5](https://github.com/lawther/smoke-optimiser/commit/d3619b58f5b8236ad23dbdb993fa0b0183f7ac62))
* resolve ty check errors and enforce type safety in models ([c3579e9](https://github.com/lawther/smoke-optimiser/commit/c3579e9805726befd28b047cc9b41b778b243ed8))
* suppress confusing coverage.py JSON report message in runner ([0d567e8](https://github.com/lawther/smoke-optimiser/commit/0d567e88054335afad192f7c8fe3c2d1dcbdfac5))


### Performance Improvements

* Lazy greedy evaluation for set cover ([b9a325c](https://github.com/lawther/smoke-optimiser/commit/b9a325c96c94e0063e9d7f18f6620e1e0b81fe42))
* optimize equivalent group calculation by utilizing frozenset dictionary keys  ([57da5a8](https://github.com/lawther/smoke-optimiser/commit/57da5a8bd02ac49fb4f87d40d33964a536697230))
* optimize greedy set cover selection algorithm in `optimise()` ([#4](https://github.com/lawther/smoke-optimiser/issues/4)) ([4318821](https://github.com/lawther/smoke-optimiser/commit/4318821fdab35abfa7c4bbf28421e1125792f6e3))
* Optimize slow test ID parsing in coverage JSON ([4cfe127](https://github.com/lawther/smoke-optimiser/commit/4cfe1272940f518ad142fa15b184a988562d9dba))
* pre-format branch strings in parser ([9cc27c8](https://github.com/lawther/smoke-optimiser/commit/9cc27c89e55836771a9643b91f2ed4e582ff2e3a))


### Documentation

* add comprehensive README with usage and argument documentation ([3666db3](https://github.com/lawther/smoke-optimiser/commit/3666db30c11014692917b63d9b69c26bd3bac8b3))
* add readme, delete impl plans ([a412bf1](https://github.com/lawther/smoke-optimiser/commit/a412bf1f15fd936157fc4331b1b482d4688d64b8))
* be super explicit about linting rules ([759fa77](https://github.com/lawther/smoke-optimiser/commit/759fa77a94a2c40873eb0e66220e90d4f8fe6253))
* plan documents ([172ef92](https://github.com/lawther/smoke-optimiser/commit/172ef92b84a19df8edbc41d9b1755476f616c456))

## Changelog
