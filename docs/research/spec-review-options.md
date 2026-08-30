# Review-Switch 的 Pre-code Spec Review 方案研究

日期：2026-08-31

> **Status:** superseded in part by `docs/design/document-review.md` (2026-08-31). This file is the research the two axes were drawn from; the design record holds the decisions, and where the two differ — status fields, basis fields, a finding cap, a finding format, the Bridge entry, the fan-out — the design record wins.

## 结论先行

审核一份准备进入实现的 spec，最核心的不是一张越来越长的 checklist，而是两个方向：

1. **Intent & Contract（意图与契约）：Build the right thing。** 白话：要做的东西是不是对的，范围、外部行为和验收标准有没有把关键决定讲清楚。
2. **Architecture & Codebase Fit（架构与代码库契合）：Fit it into the system right。** 白话：技术方案是不是长在现有系统里，Module、Interface、Seam 和测试方式是否合理，还是另起一套平行架构。

Google 的官方 code-review 指南也把整体设计与功能是否符合用户需要分开检查，并明确要求设计能融入现有系统；Kiro 的官方 Spec 工作流同样把 requirements（做什么）与 design（怎么做）作为两个需要互相校验的阶段。[来源：Google `looking-for.md`](https://github.com/google/eng-practices/blob/3bb3ec25b3b0199f4940b1aa75f0ac5c5753301c/review/reviewer/looking-for.md#L9-L21)、[Kiro Requirements-First](https://kiro.dev/docs/specs/feature-specs/requirements-first/)

这是支撑“双方向”的**来源事实**。把它们收敛成上述两个 pre-code 审查方向，是本文对 Review-Switch 的**产品推论**，不是这些来源规定的标准名称。

另外三个结论：

- **文档一致性不是第三个方向。** 它是两个方向共享的证据规则：父需求与 spec 冲突，归到 Intent & Contract；ADR、现有代码与技术方案冲突，归到 Architecture & Codebase Fit。
- **架构审查没有当前 codebase、CONTEXT/ADR 或 design artifact 就必须降级。** 没有 design artifact 就没有被审对象，reviewer 不得凭空设计；没有当前 codebase 与项目决策依据，就不能声称“与现有系统贴合”。
- **仍然只报 material blocker（会实质阻断实现的根本问题）。** 不报纯风格、假设性未来需求、没有代码库证据的“最佳实践”偏好，也不把所有边界条件排列组合成 finding。

## 两个方向分别审什么

### 方向一：Intent & Contract

它回答：

> 按明确提供的上游意图、范围和验收依据，这份 spec 是否定义了正确的交付，而且没有把重要产品决定偷偷交给实现者猜？

只关注四类高信号问题：

- **Intent / scope：** 是否解决父需求要求的问题，是否增加了未授权范围。
- **Observable contract：** 用户、调用方、数据或兼容性方面的外部结果是否明确。
- **Decision completeness：** 现有文字是否允许两个都会实质改变交付的合理实现。
- **Acceptance：** 是否存在可以判断“做到了”的外部证据。

这不是“补齐所有边界条件”。只有缺失项会造成两个 materially different outcomes（实质不同结果），并且不能安全留给实现者决定，才成为 finding。

ISO/IEC/IEEE 29148 区分 requirements verification（需求本身是否写得成立）和 requirements validation（是否定义了 stakeholder 真正需要的系统）；NASA 的需求指南也把必要性、正确性和可追踪性连接到父需求、目标及 stakeholder 依据。[来源：ISO/IEC/IEEE 29148:2018 定义 3.1.25–3.1.26](https://www.iso.org/obp/ui/#iso:std:iso-iec-ieee:29148:ed-2:v1:en)、[NASA Systems Engineering Handbook Appendix C](https://www.nasa.gov/reference/appendix-c-how-to-write-a-good-requirement/)

因此，没有上游 intent 时只能给 `spec-only` 结论：可以判断内部清晰、一致和可验收，不能声称需求“正确、完整、必要”。

### 方向二：Architecture & Codebase Fit

它回答：

> 按实际 codebase、项目 CONTEXT、适用 ADR 和架构文档，这个技术方案是在加深现有架构，还是重复责任、扩大 Interface、制造假 Seam，最终长出第二套系统？

Google 的官方指南要求 reviewer 判断变化是否属于该 codebase、是否与系统其余部分良好集成，并特别警惕为了未来假设而增加通用性。[来源：Google `looking-for.md` 的 Design](https://github.com/google/eng-practices/blob/3bb3ec25b3b0199f4940b1aa75f0ac5c5753301c/review/reviewer/looking-for.md#L9-L14)、[over-engineering](https://github.com/google/eng-practices/blob/3bb3ec25b3b0199f4940b1aa75f0ac5c5753301c/review/reviewer/looking-for.md#L46-L61)

本文采用用户指定的 `codebase-design` 作为本产品的架构判定语言。它是**本项目选择的规范依据**，不是行业共识。默认只用以下三个探针，不展开通用架构 checklist：[来源：`codebase-design` v1.2.3 的词汇与 Depth](https://github.com/mattpocock/skills/blob/v1.2.3/skills/engineering/codebase-design/SKILL.md#L8-L28)、[deletion test、Interface test surface 与真实 Seam](https://github.com/mattpocock/skills/blob/v1.2.3/skills/engineering/codebase-design/SKILL.md#L60-L65)、[`DEEPENING.md` 的 Seam 与测试纪律](https://github.com/mattpocock/skills/blob/v1.2.3/skills/engineering/codebase-design/DEEPENING.md#L27-L37)

1. **Ownership + deletion test（责任归属与删除测试）。** 白话：现有哪个 Module 已经拥有这件事？若删掉新 Module，复杂度也随之消失，它只是转发层；若复杂度会重新散落到多个调用方，它才真正提供了 locality（把变化集中在一处）。发现现有 owner 与新设计重复负责同一事实，才报“平行架构”。
2. **Interface depth + test surface（接口深度与测试面）。** 白话：调用方学很少的规则就能得到很多行为，还是新 Interface 几乎把内部实现全部暴露出来？测试应从调用方使用的同一 Interface 验证可观察结果；若 spec 必须让测试穿透 Interface 才能验证，Module 形状值得阻断。
3. **Real Seam + adapters（真实接缝与适配器）。** 白话：这里真的存在两种需要替换的接法吗？一个 adapter 只是想象中的扩展点；两个有事实依据的 adapter（常见为 production 与 test）才证明 Seam 存在。仅为了“以后可能换”而新增 port/adapter，不成为合格设计。

这三个探针只在 spec **实际新增或改变 Module、Interface、Seam** 时使用。它们不是每份 spec 都要填的三栏。一个架构 finding 还必须引用现有代码、ADR 或已拍板 design artifact；不能只说“通常应该这样”。

## 文档不一致应该怎么审

### 来源事实

GitHub Spec Kit 的 `analyze` 会跨 `spec.md`、`plan.md`、`tasks.md` 找冲突和覆盖缺口，并把 project constitution（项目最高约束文档）设为不可被静默忽略的权威；Kiro 要求 requirements、design、tasks 在修改后重新同步。[来源：Spec Kit `analyze.md`](https://github.com/github/spec-kit/blob/51e52be6c3b26fed3ff5424c671f4a559519a759/templates/commands/analyze.md#L52-L60)、[其加载的跨文档字段](https://github.com/github/spec-kit/blob/51e52be6c3b26fed3ff5424c671f4a559519a759/templates/commands/analyze.md#L75-L113)、[Kiro Best Practices](https://kiro.dev/docs/specs/best-practices/#how-do-i-iterate-on-my-feature-specs)

Kubernetes KEP 模板也把 Motivation/Goals/Non-Goals、Proposal、Design Details 和 Test Plan 放在同一条提案链中，并建议先澄清高层目标、再增量补充细节，而不是一开始卡死在所有细节上。[来源：固定版本 KEP template 的增量说明](https://github.com/kubernetes/enhancements/blob/c4f439c2dd4acb928094660be0ea771bf63f2b76/keps/NNNN-kep-template/README.md#L28-L57)、[目录结构](https://github.com/kubernetes/enhancements/blob/c4f439c2dd4acb928094660be0ea771bf63f2b76/keps/NNNN-kep-template/README.md#L86-L116)、[Motivation 与 Proposal](https://github.com/kubernetes/enhancements/blob/c4f439c2dd4acb928094660be0ea771bf63f2b76/keps/NNNN-kep-template/README.md#L176-L210)

### 产品推论

`Consistency` 不应成为第三个并发 axis，也不应生成自己的 finding 清单。它只是比较来源的方法：

| 冲突 | 归属方向 | 为什么 |
| --- | --- | --- |
| 用户原始请求 / 父 issue vs spec | Intent & Contract | 阻断的是“到底要交付什么” |
| spec 的外部行为 vs plan/tasks | Intent & Contract | 阻断的是契约或验收是否被改变、遗漏 |
| ADR / architecture doc vs 技术方案 | Architecture & Codebase Fit | 阻断的是架构决策是否被绕开 |
| 当前 codebase owner/seam vs 新 Module | Architecture & Codebase Fit | 阻断的是责任重复或平行实现 |
| 同一个冲突同时影响行为和架构 | 归到主要被阻断的方向，并在同一 finding 引用全部证据 | 不重复报两次 |

权威顺序不能由 reviewer 猜。调用者应明确哪些来源是已拍板约束；若两份来源冲突而没有权威顺序，finding 应要求 owner 选择，不能替 owner 判哪份文档“更真”。纯术语漂移只有在会导致不同契约、责任归属或任务实现时才阻断。

## 没有 codebase 或架构资料时如何降级

Architecture 审查需要两类东西：一类是“准备采用的技术方案”，另一类是“用来判断它是否贴合的现有系统”。推荐回执把两类依据分开，而不是用一个笼统的 `reviewed` 掩盖缺失：

```text
contractBasis: intent-backed | spec-only
architectureDesignBasis: spec-embedded | linked | absent
architectureFitBasis: codebase+context | codebase-only | context-only | unavailable
```

- `architectureDesignBasis = absent`：没有技术方案可审。reviewer 不得替作者发明 Module、Interface 或 Seam；Architecture 方向只能是 `not-reviewed`。
- `codebase+context`：有当前 checkout，也有适用 CONTEXT/CONTEXT-MAP、ADR 或架构文档，才能给完整的 codebase-fit 结论。
- `codebase-only`：能检查当前 owner、Interface 和 Seam；不能声称符合未提供或不存在的 ADR。
- `context-only`：只能检查方案与 CONTEXT/ADR/架构文档是否一致，不能确认这些文档仍与真实代码一致。
- `unavailable`：Architecture 方向为 `not-reviewed`；不能输出“架构 Ready”。

若本阶段明确只是 requirements-first、技术设计尚未开始，Architecture 的 `not-reviewed` 不是 finding，只表示“可以进入设计，不能进入实现”。若调用者声称已经 `ready-to-implement`，而变化显然会新增或改变 Module/Interface/Seam，却没有任何可审技术方案，这才是必须补齐的 architecture decision blocker。若只是当前 codebase/context 没有提供，则回执应为 `limited`，不能假装发现了一个架构缺陷。Kiro 同样把 requirements-first 的“确认 requirements”与后续 design feasibility review 分为两个阶段。[来源：Kiro Requirements-First](https://kiro.dev/docs/specs/feature-specs/requirements-first/)

## 代表性方案怎么做，以及哪些不应照搬

以下是代表性一手来源，不声称穷尽所有产品：

| 方案 | 一手来源事实 | 可借鉴 | 不应默认照搬 |
| --- | --- | --- | --- |
| Google Engineering Practices | code review 分开看 Design 与 Functionality，要求系统级集成，反对 speculative over-engineering | 支撑两个核心方向和“贴合现有系统” | 它审的是已写代码，不是现成的 pre-code 协议 |
| [Kiro Feature Specs](https://kiro.dev/docs/specs/feature-specs/) | 支持 Requirements-First 与 Design-First；requirements、design、tasks 会同步更新 | 明确 what/how 都要审，且依据项目阶段降级 | [Requirements Analysis](https://kiro.dev/docs/specs/analyze-requirements/) 会找 edge cases 等缺口；官方允许小型或已理解的 spec 跳过，不应把深分析变成默认入口 |
| GitHub Spec Kit | `clarify` 最多问 5 个高影响问题；`analyze` 跨 spec/plan/tasks/constitution，最多 50 个 finding | 高影响门槛、跨文档证据 | 50 项和完整 taxonomy 会形成默认清单屎山 |
| OpenSpec | pre-code review 先看 intent/scope，再看可测试性，并按风险调节力度 | 最接近短小 review 骨架 | 其固定文档格式不是所有 repo 的强制标准 |
| Kubernetes KEP | 把 why/what、how、tests、alternatives 纳入一条提案链，允许 incremental refinement | 证明成熟流程同时审需求与设计 | 其 release/production-readiness 大清单只适合 Kubernetes 级变更 |
| `codebase-design` | 用 Depth、Seam、Adapter、deletion test、Interface test surface 判断 Module 形状 | 提供少数高信号架构判定 | 它是本产品选择的规则，不应伪装成普遍行业标准 |

相关固定来源：[Spec Kit `clarify.md`](https://github.com/github/spec-kit/blob/51e52be6c3b26fed3ff5424c671f4a559519a759/templates/commands/clarify.md)、[`analyze.md`](https://github.com/github/spec-kit/blob/51e52be6c3b26fed3ff5424c671f4a559519a759/templates/commands/analyze.md)、[OpenSpec `reviewing-changes.md`](https://github.com/Fission-AI/OpenSpec/blob/a0ddb60d040c61f4907436a9d91310934b1dda63/docs/reviewing-changes.md)、[`writing-specs.md`](https://github.com/Fission-AI/OpenSpec/blob/a0ddb60d040c61f4907436a9d91310934b1dda63/docs/writing-specs.md)

## 推荐的最小审查契约

以下是基于来源的**产品推荐**，不是现有实现描述。

### 输入

必需：

- `specSource`：冻结到本次 review 的 spec。
- `stage`：`ready-to-design` 或 `ready-to-implement`；白话：这次是在确认“可以开始设计”，还是“可以开始写代码”。
- 独立 reviewer：不能让写 spec 的同一会话自审。

按方向提供：

- `intentSources`：用户原始请求、父 issue、已拍板范围和验收依据。缺失则 `contractBasis = spec-only`。
- `designSource`：spec 内嵌的技术方案，或其明确链接的 design artifact；缺失时 reviewer 不补写方案。
- `codebaseSource`：要被实现的实际 checkout。缺失就不能确认 codebase fit。
- `architectureSources`：适用 CONTEXT/CONTEXT-MAP、ADR、架构文档和项目规则；它们是判断现有系统约束的依据，不与 `designSource` 混为一类。
- `relatedArtifacts`：plan、tasks、接口契约等需要交叉核对的文档。
- `authority`：哪些来源是约束、哪些只是参考；没有声明时 reviewer 不自行猜优先级。

### 唯一 finding 门槛

一项 finding 必须同时满足：

1. 引用 spec 的具体位置；若问题来自跨来源冲突，再引用对应的上游文档、codebase 或 ADR；spec 内部矛盾则引用相互冲突的两个位置；
2. 展示两个在当前证据下都合理、但结果不同的解释或设计；
3. 差异会实质改变外部契约、数据/兼容性、安全义务、交付范围，或 Module/Interface/Seam 的责任形状；
4. 这个决定不能安全留给实现者，也不能从已提供权威来源中直接求得答案。

四条不同时满足就不报。特别是不报：

- “写得可以更完整”但说不出两个实质不同结果；
- reviewer 凭通用最佳实践发明的未来需求；
- 无 codebase/ADR 证据的架构个人偏好；
- 不改变契约、责任归属或测试面的措辞/格式问题；
- 将一个根因拆成所有失败、恢复、并发或平台组合。

### 输出

推荐顶层状态：

- `ready`：当前 `stage` 所要求的方向都完成，且没有 material blocker；`ready-to-design` 只要求 Intent & Contract，`ready-to-implement` 才要求两个方向。
- `needs-decision`：至少一个方向存在 owner 必须拍板的问题。
- `limited`：未发现 blocker，但请求的某个方向缺少依据；不能当作完整 Ready。
- `unreviewable`：spec 无法读取或为空。

每个方向另带 `basis` 和 `result: ready | needs-decision | not-reviewed`。每项 finding 只含：

```text
N. Direction: Intent & Contract | Architecture & Codebase Fit
   Evidence: <spec location + conflicting source/code location>
   Divergence: <A vs B; why materially different>
   Decision: <one question the owner can answer>
```

不设 P0/P1/P2，不输出 advisory（顺便建议），不自动改 spec。

### 数量与回合

- 推荐整份 review 最多展开 **3 个独立根因**，两个方向共享这个上限；这个数字是产品选择，不是行业标准。
- reviewer 先读完全部依据，再合并同根因。发现更多时只置 `additionalMaterialBlockers: true`，不继续列举，也不声称已穷举。
- 作者回应后最多一次 scoped re-review（只复查原 finding；仅当修订直接引入矛盾才新增）。仍保留就 `escalate`，不启动第三轮。

## 与现有 Review-Switch 的适配边界

### 已确认的现状

当前 Review-Switch 是 post-code review：Bridge 固定 Review Scope、diff/commits、spec、standards 与 code-graph 导航，再生成 Standards / Spec Axis Brief；当前 Spec 轴审“实现是否符合 spec”，不是“spec 是否可以进入实现”。[来源：当前 README](https://github.com/okqixiaobao727-design/review-switch/blob/f31fe55d3412198473bb398a03b102161ad42cbf/README.md#L3-L9)、[当前 `SPEC_BRIEF_TEMPLATE`](https://github.com/okqixiaobao727-design/review-switch/blob/f31fe55d3412198473bb398a03b102161ad42cbf/bridge/review_bridge.py#L1093-L1100)

因此可以确定：

- 不能改名复用当前 post-code `spec` 轴；两个语义不同。
- pre-code review 不能要求 diff/commit range；还没有实现 diff。
- 现有 code graph 是围绕 Review Scope 的变化导航，不能直接当成“整个 codebase 架构证据”的同义物。[来源：当前 `read_code_graph_navigation()`](https://github.com/okqixiaobao727-design/review-switch/blob/f31fe55d3412198473bb398a03b102161ad42cbf/bridge/review_bridge.py#L915-L988)
- `Consistency` 不新增 axis；finding 只标两个核心方向之一。

### 只能列为候选、不能现在断言复用

- Lane 的独立交付能力。
- spec 来源解析思路。
- report/session/receipt 持久化。
- 一次 scoped re-review 和之后升级的回合上限。

这些能力在产品语义上可能复用，但当前 `ReviewPreparation` 明确持有 Review Scope、commit list、SpecSlot、StandardsSources 与 navigation block。[来源：当前 `ReviewPreparation`](https://github.com/okqixiaobao727-design/review-switch/blob/f31fe55d3412198473bb398a03b102161ad42cbf/bridge/review_bridge.py#L1244-L1302) 直接往同一个 Interface 塞 pre-code 可选字段，可能把它变成一个浅而宽的 Module；另起完整平行 Bridge 又可能复制协议。需要单独 codebase-design 后才能决定是深化现有 Module、抽出真实共享 Seam，还是使用独立入口。**本研究不再把“同一个 Bridge 增加 review kind”写成既定架构。**

同样，两个核心方向是否由一个 reviewer 在一份报告内完成，还是两个独立 Axis Brief 并行完成，也没有一手资料能替本产品拍板。最小产品表面只要求：一次请求、两个明确方向、一个总 finding 上限、每个方向独立标记 basis；内部 fan-out 方式留待设计。

## 最小 MVP

> 在写代码前，独立 reviewer 读取冻结的 spec、明确上游 intent、实际 codebase 和适用架构资料，只沿 Intent & Contract 与 Architecture & Codebase Fit 两个方向，报告最多 3 个有双方证据、会迫使实现者做重大猜测的根因；文档冲突归入其中一个方向，缺资料就明确降级，作者回应后只复审一次。

MVP 不包含：自动改 spec、全行业 checklist、专项安全/合规审计、完整测试矩阵、第三个 Consistency 轴、自动决定文档权威顺序。

## 仍需产品拍板、尚无事实依据的选择

1. 一个 reviewer 同时审两个方向，还是两个独立 reviewer/Axis Brief；推荐先做一个请求与一份紧凑报告，但研究证据不能决定内部 fan-out。
2. 总上限是否为 3 个独立根因；3 是防膨胀的产品推荐，不是来源规定。
3. `ready / needs-decision / limited / unreviewable` 及 basis 字段的最终公开命名。
4. 入口是扩展当前 Bridge、共享若干深 Module，还是独立命令；必须先以当前 codebase/ADR 做 codebase-design，不能凭“复用听起来更好”决定。
5. `authority` 是调用者显式传入，还是按项目约定自动发现；未拍板前不能让 reviewer 自创优先级。
