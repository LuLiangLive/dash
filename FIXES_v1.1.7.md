# FIXES v1.1.7 — 看板冻结区双横线 + 缝隙修复

> 日期：2026-08-30　|　前置：v1.1.6（涨跌天数占比）
> 起因：用户报「弹窗页面冻结部分有两道横线中间是空白的，去除下面的横线；冻结区域没对接好导致有缝隙，滑动内容从中露出」

## 一、根因

`ui-migrate-v2.css`（看板自选/基金页实际生效样式）冻结机制混乱：

1. **双定位混用**：app-bar/sec-tabs/fx-tabs/tri-tabs 用 `sticky` + 硬编码 `top`（53/100px），watch-add/watch-bar 用 `fixed` + 硬编码 `top`（101/154px）。两套之间衔接靠硬编码的固定数值，**字体/布局变化时 app-bar 实际高度 ≠ 53 → 冻结区出现 3~6px 缝隙，滚动内容从缝隙露出**。
2. **双横线**：app-bar 自身 `border-bottom:1px` + sec-tabs/fx-tabs/tri-tabs 各自 `border-bottom:1px` —— 冻结区底边叠加两道横线，中间隔着 tab 按钮区。
3. `--ab-h` 已有 JS 动态同步（index.html syncAppBarHeight），但 ui-migrate-v2 的 top **完全没用**它，导致动态高度的修复对硬编码 53 无效。

## 二、修复

### 1. 去除下面那道横线（用户要求）
`ui-migrate-v2.css`：
```css
.sec-tabs,.watch-tabs, .fx-tabs, .tri-tabs { border-bottom: none; }
```
冻结区底边统一由 app-bar 的 border-bottom + watch-add 的 box-shadow 自然分隔。

### 2. 硬编码 top → 动态 var(--ab-h) 链
```css
#page-watch .sec-tabs, .fx-tabs     { top: var(--ab-h, 53px); }
.tri-tabs                            { top: calc(var(--ab-h, 53px) + 50px); }
.watch-add                          { top: calc(var(--ab-h, 53px) + 50px) !important; }
#page-watch .watch-bar              { top: calc(var(--ab-h, 53px) + 50px + 54px) !important; }
.sec-tabs,.watch-tabs               { min-height: 50px; }   /* 锁定二级 tab 高度 */
```
任何 app-bar 高度变化（字体加载/字号回退/状态栏）都会级联刷新，所有冻结块无缝衔接，**滚动内容不再从缝隙露出**。

### 3. ResizeObserver 实时同步
`index.html`：
```js
if(typeof ResizeObserver !== 'undefined'){
  new ResizeObserver(syncAppBarHeight).observe(document.querySelector('.app-bar'));
}
```
补充原有 `resize` / `fonts.ready` 之外的实时变化追踪。

## 三、验证

| 检查 | 修复前 | 修复后 |
|---|---|---|
| 冻结区横线数 | **2** 条（app-bar 底 + sec-tabs 底）| **1** 条（仅 app-bar 底） |
| watch-tabs border-bottom | 1px | **0px** |
| app-bar 实际高 53px 时各块 top | sec-tabs 53 / watch-add 101 / watch-bar 154（硬编码） | 53 / 103 / 157（动态算, 无错位） |
| 字体/布局变化时缝隙 | 3~6px 风险 | 0（ResizeObserver 实时同步） |
| 150 项测试 | — | **150 passed in 4.76s** |

截图对比：单横线版本（app-bar 一条线 + tab + 添加框阴影）替代双线版本，冻结区视觉更干净。

## 四、遗留说明

- `--tab2-h`（scheme-c-merged 用的二级 tab 变量 32px）ui-migrate-v2 不消费，无需同步。若后续切回 scheme-c-merged 布局需重新对齐。
- tri-tabs 的 top `+50px` 假设 sec-tabs/fx-tabs 高 50px，已加 min-height:50px 锁定；若调整 sec-tabs padding 需同步。
