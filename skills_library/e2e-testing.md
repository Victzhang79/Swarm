---
id: e2e-testing
title: 端到端测试模式（Playwright·页面对象）
description: "当你在用 Playwright 写端到端测试、建页面对象 POM、用 waitForResponse 替代定时等待消除 flaky 或配 trace/截图失败产物时调用，返回稳定 E2E 的配置与写法速查。"
applies_to_stacks: ["node"]
applies_to_intents: ["create", "debug"]
applies_to_phases: ["code"]
target: ["worker"]
priority: 45
max_chars: 1800
tags: ["testing", "e2e", "playwright"]
---

# E2E 测试模式速查（Playwright）

## 组织
- 目录:`tests/e2e/<域>/*.spec.ts` + `pages/`(页面对象) + `fixtures/`。
- 定位器优先用稳定属性(如 `[data-testid=...]`),勿依赖文案/CSS 层级。

## 页面对象(POM)
```ts
export class ItemsPage {
  constructor(private page: Page) {}
  search = this.page.locator('[data-testid="search-input"]')
  cards  = this.page.locator('[data-testid="item-card"]')
  async goto(){ await this.page.goto('/items'); await this.page.waitForLoadState('networkidle') }
  async doSearch(q:string){
    await this.search.fill(q)
    await this.page.waitForResponse(r => r.url().includes('/api/search'))
  }
}
```
- 用例用 `test.describe` + `beforeEach` 初始化页面对象。

## 稳定性(消除 flaky)——核心价值
- 用自动等待的 locator 动作,勿 `page.click(sel)` 假设已就绪。
- 等具体条件而非定时:用 `waitForResponse(r=>r.url().includes(...))` / `locator.waitFor({state:'visible'})`,禁用 `waitForTimeout(ms)`。
- 动画:先 `waitFor visible` + `waitForLoadState('networkidle')` 再交互。
- 排查:`--repeat-each=10` / `--retries=3` 复现;暂隔离用 `test.fixme` 并挂踪迹号,勿默默 skip。

## 配置要点
- `retries: CI?2:0`,`workers: CI?1:undefined`,`fullyParallel: true`。
- `use`: `trace:'on-first-retry'`,`screenshot:'only-on-failure'`,`video:'retain-on-failure'`;设 `actionTimeout`/`navigationTimeout`。
- `webServer` 自起被测服务,`reuseExistingServer:!CI`。
- reporter 出 html + junit(供 CI 汇总)。

## 产物与危险流
- 失败留证:截图 `page.screenshot({path})`、trace、video,CI 里 `if: always()` 上传。
- 涉真钱/生产副作用的用例:`test.skip(process.env.NODE_ENV==='production', ...)`;外部依赖(钱包/链/第三方)用 `context.addInitScript` 注入 mock provider。
