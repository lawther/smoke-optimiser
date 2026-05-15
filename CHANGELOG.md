# Changelog

## [0.3.0](https://github.com/lawther/smoke-optimiser/compare/v0.2.0...v0.3.0) (2026-05-15)


### Features

* add version-controlled git hooks and auto-install on precommit ([05e3118](https://github.com/lawther/smoke-optimiser/commit/05e31186345f213b1773b6549b30161299aefedc))


### Bug Fixes

* validate external data with Pydantic to satisfy ML400 lint ([e33671b](https://github.com/lawther/smoke-optimiser/commit/e33671b65018f59214da4b785c573910dca1aa68))

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
