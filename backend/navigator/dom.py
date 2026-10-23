"""JavaScript injected into pages: element extraction, state signature, Set-of-Marks, static audit."""

# Tags every interactive element with data-sim-id and returns a compact description of the page.
EXTRACT_JS = r"""
(opts) => {
  const MAX = opts.max || 70;
  document.querySelectorAll('[data-sim-id]').forEach(e => e.removeAttribute('data-sim-id'));
  const sel = 'a[href],button,input:not([type=hidden]),select,textarea,summary,[role=button],[role=link],[role=checkbox],[role=tab],[role=menuitem],[role=switch],[role=radio],[role=option],[onclick],[tabindex]:not([tabindex="-1"])';
  const cands = new Set(document.querySelectorAll(sel));
  document.querySelectorAll('div,span,li,img,svg,td,label,p').forEach(e => {
    try {
      if (getComputedStyle(e).cursor === 'pointer' && !(e.parentElement && getComputedStyle(e.parentElement).cursor === 'pointer')) cands.add(e);
    } catch (_) {}
  });
  const parseRGB = s => { const m = (s||'').match(/rgba?\(([^)]+)\)/); if (!m) return null; const p = m[1].split(',').map(x => parseFloat(x)); return {r:p[0], g:p[1], b:p[2], a: p.length > 3 ? p[3] : 1}; };
  const lum = c => { const f = v => { v/=255; return v <= 0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055, 2.4); }; return 0.2126*f(c.r)+0.7152*f(c.g)+0.0722*f(c.b); };
  const bgOf = el => { let e = el; while (e) { const c = parseRGB(getComputedStyle(e).backgroundColor); if (c && c.a > 0.5) return c; e = e.parentElement; } return {r:255,g:255,b:255,a:1}; };
  const contrast = el => { const fg = parseRGB(getComputedStyle(el).color); if (!fg) return null; const bg = bgOf(el); const l1 = lum(fg), l2 = lum(bg); return (Math.max(l1,l2)+0.05)/(Math.min(l1,l2)+0.05); };
  const visible = (el, r) => { const s = getComputedStyle(el); return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none' && parseFloat(s.opacity) > 0.05; };
  const labelOf = el => {
    const t = (v) => (v || '').replace(/\s+/g, ' ').trim().slice(0, 70);
    let n = t(el.getAttribute('aria-label'));
    if (!n && el.getAttribute('aria-labelledby')) n = t(el.getAttribute('aria-labelledby').split(' ').map(i => (document.getElementById(i)||{}).innerText).join(' '));
    if (!n && el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) n = t(l.innerText); }
    if (!n && el.closest && el.closest('label')) n = t(el.closest('label').innerText);
    if (!n && ['INPUT','TEXTAREA','SELECT'].includes(el.tagName)) n = t(el.placeholder) || t(el.name);
    if (!n && el.tagName === 'INPUT' && ['submit','button'].includes(el.type)) n = t(el.value);
    if (!n) n = t(el.innerText);
    if (!n) n = t(el.title);
    if (!n) { const img = el.querySelector && el.querySelector('img[alt]'); if (img) n = t(img.alt); }
    return n;
  };
  const out = [];
  let id = 0;
  for (const el of cands) {
    const r = el.getBoundingClientRect();
    if (!visible(el, r)) continue;
    if (el.closest('[data-sim-skip]')) continue;
    id += 1;
    el.setAttribute('data-sim-id', String(id));
    const tag = el.tagName.toLowerCase();
    const type = el.getAttribute('role') || (tag === 'input' ? (el.type || 'text') : tag);
    const name = labelOf(el);
    const hasIcon = !!(el.querySelector && el.querySelector('svg,img,i,[class*=icon]'));
    const inView = r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth;
    let covered = '';
    if (inView) {
      const cx = Math.min(Math.max(r.left + r.width/2, 1), innerWidth-1), cy = Math.min(Math.max(r.top + r.height/2, 1), innerHeight-1);
      const top = document.elementFromPoint(cx, cy);
      if (top && top !== el && !el.contains(top) && !top.contains(el)) covered = (top.id ? '#' + top.id : top.tagName.toLowerCase()) + ' "' + (top.innerText||'').replace(/\s+/g,' ').trim().slice(0, 40) + '"';
    }
    const c = contrast(el);
    out.push({
      id, tag, type, name, icon_only: (!name && hasIcon) || undefined,
      value: ['INPUT','TEXTAREA','SELECT'].includes(el.tagName) && el.type !== 'password' ? (el.value || '').slice(0, 40) : undefined,
      options: tag === 'select' ? Array.from(el.options).slice(0, 8).map(o => o.text.trim().slice(0, 25)) : undefined,
      disabled: el.disabled || el.getAttribute('aria-disabled') === 'true' || undefined,
      checked: el.checked || undefined,
      required: el.required || undefined,
      readonly: el.readOnly || undefined,
      offscreen: !inView || undefined,
      covered: covered || undefined,
      small: Math.min(r.width, r.height) < 30 || undefined,
      low_contrast: (c !== null && c < 3 && name) ? Number(c.toFixed(2)) : undefined,
    });
    if (id >= MAX) break;
  }
  // hidden / readonly fields: invisible to ordinary users, tamper targets for the integrity persona
  const hidden = [];
  document.querySelectorAll('input[type=hidden], input[readonly]').forEach(el => {
    if (hidden.length >= 12) return;
    id += 1; el.setAttribute('data-sim-id', String(id));
    hidden.push({id, name: el.name || el.id || '(unnamed)', value: (el.value || '').slice(0, 40), kind: el.type === 'hidden' ? 'hidden' : 'readonly'});
  });
  const alerts = Array.from(document.querySelectorAll('[role=alert],[aria-live],.error,.alert,.toast,.notification,.flash,[class*=error],[class*=toast]'))
    .map(e => (e.innerText || '').replace(/\s+/g, ' ').trim()).filter(t => t && t.length < 200).slice(0, 5);
  const text = (document.body ? document.body.innerText : '').replace(/[ \t]+/g, ' ').replace(/\n{2,}/g, '\n').trim();
  return {
    url: location.href, title: document.title, elements: out, hidden, alerts,
    text: text.slice(0, opts.textMax || 1500), text_len: text.length,
    scroll: {y: Math.round(scrollY), max: Math.max(0, document.documentElement.scrollHeight - innerHeight)},
  };
}
"""

# Cheap signature used to detect "did anything change after my action?"
SIGNATURE_JS = r"""
() => {
  const t = document.body ? document.body.innerText : '';
  let h = 0; for (let i = 0; i < t.length; i++) h = (h * 31 + t.charCodeAt(i)) | 0;
  return {url: location.href, h, n: document.querySelectorAll('*').length,
          dis: document.querySelectorAll(':disabled').length, y: Math.round(scrollY)};
}
"""

# Draw numbered boxes over interactive elements (Set-of-Marks) for the vision grounder.
SOM_ON_JS = r"""
() => {
  const wrap = document.createElement('div'); wrap.id = '__som__'; wrap.setAttribute('data-sim-skip', '1');
  wrap.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:2147483647';
  document.querySelectorAll('[data-sim-id]').forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth) return;
    const b = document.createElement('div');
    b.style.cssText = `position:fixed;left:${r.left}px;top:${r.top}px;width:${r.width}px;height:${r.height}px;border:2px solid #e11;box-sizing:border-box`;
    const l = document.createElement('span'); l.textContent = el.getAttribute('data-sim-id');
    l.style.cssText = 'position:absolute;left:0;top:-14px;background:#e11;color:#fff;font:bold 12px sans-serif;padding:0 3px';
    b.appendChild(l); wrap.appendChild(b);
  });
  document.body.appendChild(wrap);
}
"""
SOM_OFF_JS = "() => { const w = document.getElementById('__som__'); if (w) w.remove(); }"

# Static accessibility / mobile audit. Deterministic, costs zero LLM tokens.
AUDIT_JS = r"""
() => {
  const issues = [];
  const add = (kind, count, sample, severity) => { if (count > 0) issues.push({kind, count, sample: sample.slice(0, 3), severity}); };
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const parseRGB = s => { const m = (s||'').match(/rgba?\(([^)]+)\)/); if (!m) return null; const p = m[1].split(',').map(x => parseFloat(x)); return {r:p[0], g:p[1], b:p[2], a: p.length > 3 ? p[3] : 1}; };
  const lum = c => { const f = v => { v/=255; return v <= 0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055, 2.4); }; return 0.2126*f(c.r)+0.7152*f(c.g)+0.0722*f(c.b); };
  const bgOf = el => { let e = el; while (e) { const c = parseRGB(getComputedStyle(e).backgroundColor); if (c && c.a > 0.5) return c; e = e.parentElement; } return {r:255,g:255,b:255,a:1}; };
  const short = el => (el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + ' "' + (el.innerText || el.alt || el.name || '').replace(/\s+/g,' ').trim().slice(0, 30) + '"');

  const imgs = Array.from(document.images).filter(i => vis(i) && !i.hasAttribute('alt'));
  add('Images without alt text', imgs.length, imgs.map(i => (i.src || '').split('/').pop()), 'medium');

  const fields = Array.from(document.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]),select,textarea')).filter(vis)
    .filter(el => !(el.getAttribute('aria-label') || el.getAttribute('aria-labelledby') || (el.id && document.querySelector('label[for="' + CSS.escape(el.id) + '"]')) || el.closest('label')));
  add('Form fields without a visible label (placeholder only)', fields.length, fields.map(short), 'medium');

  const unnamed = Array.from(document.querySelectorAll('button,a[href],[role=button]')).filter(vis)
    .filter(el => !((el.innerText || '').trim() || el.getAttribute('aria-label') || el.title || (el.querySelector('img[alt]') || {}).alt || el.getAttribute('aria-labelledby')));
  add('Buttons/links with no accessible name (icon-only)', unnamed.length, unnamed.map(short), 'high');

  if (!document.documentElement.lang) add('Page has no lang attribute', 1, ['<html>'], 'low');
  if (!document.querySelector('h1')) add('Page has no <h1> heading', 1, ['-'], 'low');
  const vp = document.querySelector('meta[name=viewport]');
  if (vp && /user-scalable\s*=\s*(no|0)|maximum-scale\s*=\s*1(\.0)?\b/.test(vp.content)) add('Pinch-zoom disabled (hurts low-vision users)', 1, [vp.content], 'medium');

  const low = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const seen = new Set(); let n;
  while ((n = walker.nextNode()) && seen.size < 600) {
    const el = n.parentElement; if (!el || seen.has(el) || !n.textContent.trim() || !vis(el)) continue;
    seen.add(el);
    const fg = parseRGB(getComputedStyle(el).color); if (!fg) continue;
    const l1 = lum(fg), l2 = lum(bgOf(el)); const ratio = (Math.max(l1,l2)+0.05)/(Math.min(l1,l2)+0.05);
    const size = parseFloat(getComputedStyle(el).fontSize);
    if (ratio < (size >= 24 ? 3 : 4.5)) low.push({el, ratio});
  }
  add('Low colour contrast text (WCAG AA fails)', low.length, low.sort((a,b) => a.ratio-b.ratio).map(x => short(x.el) + ' ratio ' + x.ratio.toFixed(1)), 'medium');

  const tiny = Array.from(document.querySelectorAll('a[href],button,input:not([type=hidden]),select,[role=button]')).filter(vis)
    .filter(el => { const r = el.getBoundingClientRect(); return Math.min(r.width, r.height) < 32; });
  add('Tap targets smaller than 32px', tiny.length, tiny.map(short), 'low');

  const tinyText = Array.from(seen).filter(el => parseFloat(getComputedStyle(el).fontSize) < 12);
  add('Text smaller than 12px', tinyText.length, tinyText.map(short), 'low');

  if (document.documentElement.scrollWidth > innerWidth + 8) add('Page scrolls sideways (horizontal overflow)', 1, ['scrollWidth ' + document.documentElement.scrollWidth + ' > viewport ' + innerWidth], 'medium');
  return issues;
}
"""

# Fills empty plain text fields with plausible valid data (integrity persona shortcut; never touches card/password fields).
FILL_JS = r"""
() => {
  let n = 0;
  document.querySelectorAll('input:not([type=hidden]):not([type=password]):not([type=checkbox]):not([type=radio]):not([type=submit]):not([type=button]),textarea').forEach(el => {
    const r = el.getBoundingClientRect();
    if (el.value || el.readOnly || el.disabled || r.width < 2 || r.height < 2) return;
    const hint = ((el.name || '') + ' ' + (el.id || '') + ' ' + (el.placeholder || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' + (el.type || '')).toLowerCase();
    if (/card|cvv|cvc|expir|iban|otp|coupon|promo|code/.test(hint)) return;
    let v = 'Test User';
    if (/mail/.test(hint)) v = 'tester@example.com';
    else if (/phone|mobile|tel/.test(hint)) v = '9876543210';
    else if (/pin|zip|postal/.test(hint)) v = '682001';
    else if (/address|street|addr/.test(hint)) v = '12 Test Street, Kochi';
    else if (/city|town/.test(hint)) v = 'Kochi';
    else if (el.type === 'number') v = '1';
    el.focus(); el.value = v;
    el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true}));
    n++;
  });
  return n;
}
"""
