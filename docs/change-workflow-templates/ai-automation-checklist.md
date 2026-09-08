# AI 变更自动调用检查清单

> 版本：v1.0 | 适用：每次变更必须逐项通过

## 变更启动阶段
- [ ] 已识别变更场景（用户表达了修改/新增/优化/重构/修复意图）
- [ ] 已自动调用 `change-workflow.mjs start` 启动流程
- [ ] 已完成苏格拉底式7问需求分析
- [ ] 已完成替代方案对比

## 任务分解阶段
- [ ] 已自动调用 `change-workflow.mjs update --stage writing-plans`
- [ ] 任务已分解到2-5分钟粒度
- [ ] 文件路径已精确到具体文件
- [ ] YAGNI/DRY检查已通过

## UI设计阶段（仅UI变更）
- [ ] 已自动调用 `ui-design-guide.mjs` 获取设计命令
- [ ] 已阅读impeccable 23条设计原则中相关条目
- [ ] 设计方案已遵循设计命令指导

## 编码实现阶段
- [ ] 已自动调用 `change-workflow.mjs update --stage subagent-dev`
- [ ] 含中文文件已用Python脚本修改（encoding='utf-8'）
- [ ] 未修改FundCard.vue的硬编码颜色和字号
- [ ] 未引入AI推理神经网络（保持功能神经网络定位）
- [ ] 未引入新的npm依赖

## UI检测阶段（仅UI变更）
- [ ] 已自动调用 `change-ui-check.mjs` 检测变更文件
- [ ] P0问题已修复或记录豁免
- [ ] 检测结果已记录到变更历史

## 构建验证阶段
- [ ] 已运行 `npm run build`（prebuild+postbuild自动触发）
- [ ] prebuild验证已通过（或警告已记录）
- [ ] postbuild验证已通过（或警告已记录）
- [ ] dist目录已生成
- [ ] 已自动调用 `change-workflow.mjs update --stage verification`

## 完成收尾阶段
- [ ] 已自动调用 `change-workflow.mjs finish`
- [ ] change_history.json已新增记录
- [ ] 版本号已升级
- [ ] 神经网络已同步（涉及节点version和last_verified）
- [ ] 部署包已生成
- [ ] 后端服务已重启并验证可访问

## 异常处理检查
- [ ] 工具调用失败时已尝试修复或记录豁免
- [ ] 文件不存在时已显示空状态而非报错
- [ ] 构建失败时已定位并修复
- [ ] 未让用户手动运行命令行脚本

## 通过标准
所有适用项必须为 [x]，不适用项标注 N/A。存在未通过项时，变更不得标记为完成。
