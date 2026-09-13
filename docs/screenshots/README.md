# README 软件截图

这些图片来自 2026-09-09 保存的本地界面回归截图，于 2026-09-13 整理用于项目 README。对应代码已包含在初始发布提交 `4e97e8f` 中；本次仅更新文档和截图资产，没有重新运行截图采集，也没有修改界面内容。

所有患者、住院号、医务人员及病历文字均来自项目的虚构开发数据或测试输入。图片展示实际渲染界面，不是设计稿，不证明真实患者使用、模型准确率、降噪效果或医院接口验收。

| 文件 | 原始截图（仓库本地路径，不随仓库发布） | 处理 | 场景 |
| --- | --- | --- | --- |
| [workspace-desktop.png](workspace-desktop.png) | `.local/e2e/desktop-admission.png` | 从左上角裁切 1440 × 1000 | 医生工作台，人工填写的虚构入院记录 |
| [chapter-navigation.png](chapter-navigation.png) | `frontend/.local/note-usability/desktop-navigation.png` | 原样复制 | 长文书章节定位、未保存状态与缺项 |
| [capture-resilience.png](capture-resilience.png) | `.local/capture-quality-review/desktop-integrated.png` | 原样复制 | 合成原生桥事件：双声道电平、低电平和上传中断提示 |
| [note-history.png](note-history.png) | `.local/e2e/desktop-history.png` | 从左上角裁切 1440 × 1000 | 不可变文书版本弹窗，背景虚化由应用自身产生 |
| [mobile-workspace.png](mobile-workspace.png) | `.local/e2e/mobile-workspace.png` | 从左上角裁切 390 × 844 | 移动浏览器工作台，非原生移动客户端 |
| [mobile-source-review.png](mobile-source-review.png) | `frontend/.local/note-usability/mobile-source-review.png` | 原样复制 | 章节来源选择与核对说明；HTTP 503 由回归测试注入 |

没有对界面文字、状态、错误提示或人物信息做后期替换。裁切只去除全页截图中超出展示区域的部分；未拉伸图片。原始截图仍保留在已被 Git 忽略的本地验证目录中。

界面回归入口见 [系统流程测试](../../tests/e2e/workspace.mjs) 和 [文书操作回归](../../frontend/tests/note-usability.mjs)。历史验证结果及设备、真实模型的验收边界见 [实现与验收记录](../implementation-progress.md)。
