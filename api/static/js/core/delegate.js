/* Swarm Web UI — core/delegate module（30 号文批11 D-1②/D-2）
 *
 * 事件委托内核：替代全部内联 on<event>="..." 属性 handler。两成立意：
 * ① CSP `script-src 'self'` 禁用一切内联事件属性（D-2 安全响应头的前置）；
 * ② 旧式内联 handler 的属性内插值是 JS 字符串上下文注入面（D-1），
 *    迁到 data-* 属性后值永远只是字符串（HTML 属性上下文，escapeAttr 分档转义），
 *    不再经过 JS 解析。
 *
 * 约定（尖括号内为占位——勿写成带引号的字面示例，静态闸会把注释里的示例当真 handler 校验）：
 *   <button data-on-click=某全局函数名 data-arg0="a" data-arg1="b">
 *   <input  data-on-change=某全局函数名 data-arg0="rerank">
 * - handler 名支持点路径（如 PlanningInteraction.skipClarify），派发时在 window 上解析；
 * - 调用形态 fn.apply(el, [...args, event])：this=命中元素，event 追加为末参——
 *   旧式 `fn(event)` / `fn(this.value)` 形态自然衔接；
 * - data-argN 的值默认字符串；静态数值/布尔参数用 data-argN-t="n"/"b" 标记还原类型
 *   （dataset 读回一律是字符串，"false" 是 truthy——类型标记防静默语义漂移）；
 * - data-stop-prop="1"：派发前 event.stopPropagation()（替代内联多语句前缀）。
 * - 未知名 = console.error 明示（fail-loud，不静默吞）。
 */
'use strict';

(function () {
  const DELEGATED_EVENTS = ['click', 'change', 'input', 'submit', 'keydown', 'keyup',
                            'dragover', 'dragleave', 'drop'];

  function _resolveHandler(name) {
    let obj = window;
    for (const part of String(name).split('.')) {
      obj = obj ? obj[part] : undefined;
    }
    return typeof obj === 'function' ? obj : null;
  }

  function _collectArgs(el) {
    const args = [];
    for (let i = 0; ; i++) {
      const v = el.getAttribute('data-arg' + i);
      if (v === null) break;
      const t = el.getAttribute('data-arg' + i + '-t');
      if (t === 'n') args.push(Number(v));
      else if (t === 'b') args.push(v === 'true');
      else args.push(v);
    }
    return args;
  }

  for (const type of DELEGATED_EVENTS) {
    document.addEventListener(type, (e) => {
      const attr = 'data-on-' + type;
      const el = e.target && e.target.closest ? e.target.closest('[' + attr + ']') : null;
      if (!el) return;
      if (el.getAttribute('data-stop-prop') === '1') e.stopPropagation();
      const name = el.getAttribute(attr);
      const fn = _resolveHandler(name);
      if (!fn) {
        console.error('[delegate] 未注册的全局 handler（data-' + 'on-' + type + '）:', name);
        return;
      }
      fn.apply(el, [..._collectArgs(el), e]);
    });
  }
})();
