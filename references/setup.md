# 部署与配置

## 前提

- macOS 14 或更高；Windows、Linux、iPhone 单独运行不支持。
- 稳定安装的 Python 3.9+、Xcode Command Line Tools（`xcrun clang`）、`codesign`。
- iPhone 短信已通过用户自己的系统设置同步到 Mac；后台不能从手机补拉尚未同步的短信。
- 使用者愿意授予运行程序所需的完全磁盘访问、提醒事项权限；启用日历时还需日历完整访问。权限必须由使用者在系统界面授予，不能修改权限数据库。
- 在“提醒事项”中建立一个名称唯一的目标列表。日历可选，启用时选择名称唯一且可写的日历。
- 本包内置 pypdf 6.10.0 及其许可证。OCR 不内置；可配置已有的 `mineru-local` 可执行文件，缺少OCR且传票无法识别时转人工。

## 导入 Skill

解压后将 `court-sms-automation` 整个文件夹放入接收者自己的 `~/.codex/skills/`。已有同名目录时先比较版本并保留副本，不能直接覆盖。也可以先让 Codex 读取解压目录内的 SKILL.md，不安装即审阅。

示例请求：
> 使用 court-sms-automation，先检查我的 Mac 是否具备安装条件。我确认起始日期和保存目录后再启用。

## 准备安装（不会启动后台）

在 skill 目录执行，先把日期、目录和列表改成使用者确认的值：

```sh
python3 scripts/manage.py prepare \
  --start-date YYYY-MM-DD \
  --output "$HOME/Documents/法院材料" \
  --reminder-list '法院材料'
```

默认部署到 `~/Library/Application Support/CourtSMSAutomation`。`--target` 可以更改，但不得选共享同步目录或已有后台的状态目录。`prepare` 拒绝覆盖非空目录，不修改既有安装。

可选参数：
- `--calendar '指定日历名称'`：启用自动日历；不传则只处理材料与提醒。
- `--mineru '/本机已有的OCR命令路径'`：启用本地OCR调用，不自动下载模型或安装工具。

准备流程会复制运行源代码，编译并本地签名提醒组件（需要日历时也构建日历组件），创建配置、空状态及恢复副本，并生成待启用的 LaunchAgent 文件。不读取/复制旧机器状态，不下载材料，不启动日历，不写入提醒。

检查 `runtime/config.json`：

| 字段 | 含义 |
|---|---|
| start_date | 按本机时区解释的短信接收日期起点；不是案号年份 |
| output_dir | 材料归档目录 |
| state_dir | 本机私有运行状态目录；建议保留本地磁盘位置 |
| chat_db | 本机短信库路径，程序只读访问 |
| reminder_list | 用户确认的唯一提醒列表名称 |
| calendar_enabled / calendar_name | 可选日历开关及目标日历 |
| mineru_bin | 可选本地OCR路径 |

## 验证与启用

```sh
python3 scripts/manage.py doctor
```

doctor 为只读诊断，输出范围内最新短信编号、扫描时间、队列和最近最多20份送达清单的哈希检查，不输出短信正文或访问参数。无法读取数据库时说明权限检查未通过；不能据此认定短信已丢失。

用户授权后台处理、确认时间范围后，先在用户在场时执行一次正式运行，接受必要的系统权限弹窗：

```sh
python3 "$HOME/Library/Application Support/CourtSMSAutomation/runtime/court_sms_monitor.py"
```

这一步会真实下载、写入提醒和可选日历，不是只读测试。检查生成文件、提醒、人工待办；权限授予在终端成功不保证 LaunchAgent 身份下也成功。

随后启用：

```sh
python3 scripts/manage.py activate
```

默认任务标签 `local.court-sms-automation`，间隔60秒、登录时运行。电脑休眠或用户未登录时不能保证准时执行。再次核对实际后台运行、数据库权限、提醒回读；不要单凭启动命令返回成功验收。

此后台不依赖 Codex 每分钟运行。Skill 是安装/维护入口；真正的定时处理由本机 LaunchAgent 完成。本包不自动创建任何 Codex 晨检或跟进自动化。
