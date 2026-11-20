# 文书审查与日历接口协议

本文件是公开源码的集成接口说明。运行规则见[技能入口](../../SKILL.md)、[运行与验收](../../references/operations.md)和[模型审查协议](../../references/review-prompt.md)。先配置接收者的路径、依赖、权限、处理范围及后台运行方式；不能继承其他电脑的授权、日期范围、日历或部署成功结论。

## 控制器顺序

1. 读取队列和模型健康；先只读恢复 partial 日历回执及有限量附件同步待核。
2. 先按原件 SHA-256 复用同目录 `OCR文本/<原件主名>.md`。逐页直接提取完整可靠的 PDF 文字层。其余页默认使用夸克 general-ocr，住宿发票用 vat-invoice-ocr，铁路电子客票用 general-ocr。本地 OCR 仅复核异常页。手写、签章和关键数字须对照原图，逐页记录原件指纹、实际引擎及复核状态。原件变化后更新，复核记录只适用于对应指纹和页码。
3. 阅读全部正文和必要真实原图记录，以完整案号补充历史材料及获授权短信中的主体信息。
4. 实际加载适用技能、查询法律 MCP，以 `gpt-6.1-sol` / `max` 形成结构化全文决定并独立复核。
5. 核对引用、页数、主体、起算、复算、权限及已有 UID；旧规则 ready 决定重审，不原样恢复写入。
6. 先记录各文书全文决定，再记录/写入同案同真实收到日的收到组，最后处理独立到期和活动。
7. 持锁写入，按 UID 回读全部字段、真实附件 SHA 和提醒。失败保留待办与原 UID，不清队列、不重复添加。

## 台账命令

以下命令只在已配置环境中运行；DOCID、META.json 等为占位参数。先使用只读命令核对范围，改变台账和日历须有对应授权，不手工编辑状态来伪造完成。

```text
python3 court_document_review.py status
python3 court_document_review.py pending --limit 20
python3 court_document_review.py context '完整案号'
python3 court_document_review.py register-ocr DOCID META.json
python3 court_document_review.py review DOCID DECISION.json
python3 court_document_review.py apply DOCID
python3 court_document_review.py receipts DOCID RECEIPTS.json
```

OCR META 含 text_path、engine、skill_path、page_count、source_hash。源文件或 OCR 内容变化后重新核验。

全文 DECISION 含 calendar_policy_version:4、source_sha256、真实完整阅读下的 all_pages_reviewed:true、date_inventory、conclusion、unresolved、pending_checks 和 events。模型 events 只输出确定 deadline/appointment，并以 receipt_materials 列具体期限材料；程序从精确 receipt_binding 取得收到日期。internal_check 只进待核，不生成待核到期日。

apply 串行调用 Calendar 桥并持久保存回执，不绕过控制器并行写入。接口以当前源码及测试为准；公开包不含安装器，也没有启用后台或新建定时任务的隐含动作。

## 事件与收到组

控制器事件含 event_key、case_number、event_kind（receipt/deadline/appointment）、category、action_label、title、start/end（含正确时区）、all_day、location、notes、source_quote、source_page、basis_type、parties_status、attachment_paths。主体确认需 parties 和 party_sources；计算到期另需真实起算证据、算式、法源、节假日及交叉核对。

receipt 需 received_date、received_evidence、receipt_documents，稳定键为“完整案号|收到材料|YYYY-MM-DD”；deadline 需 received_date、due_date、paired_receipt_key 及 alarm_minutes:-10080。两个节点备注写收到/到期，未知到期写待核；两节点均真实附相关 PDF 及跨文书依据。收到节点不另加期限提醒，保留默认及人工提醒。

收到组独立使用 record_receipt_group_review(group_key, decision) 和 apply_receipt_group_calendar(group_key, fast_lookup=False)。组决定为 calendar_policy_version:4、receipt_only:true、单条 receipt，all_pages_reviewed 不得为 true。receipt_groups 与成员全文状态分别保存，旧单文书 receipt 接口仅兼容既有记录，不作为新组入口。

每项 receipt_documents 含 id、sha256、source_path、binding、source_page、source_quote、material_type、review_basis。依据须为：

- material_page_review：实际看过的材料类型页，含 source_sha256、page、source_quote、material_type、method:"visual"、verified_at、review_record。
- verified_receipt：引用旧已回读 receipt_event_key。
- complete_document_review：引用当前完整决定的相同页码及原句。

不能把文字核对改称视觉核验，局部收到不改变全文 review_status 或 OCR 质量。后续批次保留旧组全部已核验成员、PDF、到期及待核项，不由最后一份覆盖；同 SHA 重复通知不重置收到或起算。

## Calendar、附件与恢复

Calendar 桥读取输入为 {"mode":"read","from":"含时区时间","to":"含时区时间","case_number":"完整案号"}，写入为 {"mode":"apply","events":[...]}。控制器通过 osascript -l JavaScript court_review_calendar.js PLAN.json RECEIPTS.json 调用，并经正常 macOS 应用入口运行附件助手。

标题、日期、地点、全天、简洁备注、实际附件内容 SHA 和提醒全部通过回读才确认。附件不可读时未完成，不以输入原件 SHA 替代回读结果。已有 partial 先按完整原方案只读检查，满足则补回执，不满足保留原因和原 UID，不先重复追加。

保留人工标题信息、自动区块外备注和既有提醒；只改标题时其他字段不动。活动性质要回源，不从旧标题猜测。批量检查从实际指定日历与新旧台账并集出发。

本机真实附件、云端地址登记、另一台 Mac 实际接收是三个状态。extras_verified 不证明云端，uploaded/remote_url_registered 不证明另一设备接收；不公开附件访问 URL。同步重试只读，不因缺远端地址重复 add。修复需独立具体计划和授权，核对唯一 UID、最新完整元数据/附件清单及准确原件 SHA；没有任何私案固定修复授权随本包发布。

## 启动与健康

scripts/workflow_worker.py 每次运行一轮后退出，支持 --max-cases、--preview、--case、--probe-model。模型探针仅证明可调用，不能代替案卷及日历验收。launch_worker.py 与系统服务需按接收者环境接入，源码存在不代表服务已安装或启用。

分别检查模型健康、各轮结果和 OCR/全文/收到/日历状态。失败保留并重试；进程退出 0 或扫描追平不代表完成。回退不覆盖新回执，不自动删除日程。

网站补查遵循[法院网站辅助核对](../../references/court-website.md)，不自动创建定时聊天。网页查询日不作收到日，网页通用标签不覆盖通知原件性质，查询授权不扩大为签收或诉讼提交。
