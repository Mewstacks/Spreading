---
target: UX/UI review completo do app
total_score: 21
p0_count: 0
p1_count: 4
timestamp: 2026-07-10T02-11-13Z
slug: python-django-apps-templates
---
# Critique — Spreading app UI (all authenticated pages + auth)

Target: python/django/apps/templates (live at localhost:8000, mobile 390px + desktop 1440px)

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Good pills/steppers/live logs; sync buttons give no feedback; WhatsApp error state has no retry action |
| 2 | Match System / Real World | 2 | Raw enums in UI ("whatsapp", "mercadolivre"), jargon (grupo_id, macro-categoria, credential), infra leaks ("porta 3000", "Fly.io") |
| 3 | User Control and Freedom | 2 | Rules cannot be edited — only deleted and recreated; native confirm(); no undo |
| 4 | Consistency and Standards | 2 | Amazon creds editable in 2 pages, ML connect in 2 places, nav "Envios" vs title "Configurações de Envio", save buttons green/blue inconsistent |
| 5 | Error Prevention | 2 | janela_inicio/fim unvalidated pair, free-text destination ids, no inline validation |
| 6 | Recognition Rather Than Recall | 2 | "Começar" onboarding page orphaned (no link anywhere); 12-item nav; group id must be recalled when service offline |
| 7 | Flexibility and Efficiency | 2 | No rule edit, no bulk send, one 10-field form per rule; filter persistence is good |
| 8 | Aesthetic and Minimalist Design | 2 | Solid token system undermined by chatty dev-voice microcopy under every control; empty-table headers; mobile filter card fills first viewport |
| 9 | Error Recovery | 2 | Raw "[ERRO]" log lines as UI, "Conexão perdida" dead-end; ML reconnect link is good |
| 10 | Help and Documentation | 2 | Telegram inline steps good; nothing contextual elsewhere |
| **Total** | | **21/40** | **Acceptable — significant improvements needed** |

## Anti-Patterns Verdict

LLM: does not read as generic AI slop visually (tokens, dark/light theme, mobile card tables are competent), but reads as **dev-built, not product-built**: placeholder-as-instruction everywhere ("cole o secret", "12345@g.us", "deixe vazio p/ padrão"), em-dash chatty fragments as microcopy ("— salva aqui", "— sem QR, sem app aberto"), raw enum values ("whatsapp", "Piloto · trial"), infra vocabulary in user-facing errors ("serviço Node na porta 3000", "Suba o serviço spreading-wa no Fly.io"), emoji in success copy, mojibake hidden span in home.html:40 ("Sua operaÃ§Ã£o").

Detector (8 findings): broken `<img id="qr-img">` ships without src (confirmed live as broken-image box on WhatsApp page); `transition: width` on progress bars (base.html:296, dashboard.html:23); colored glow on logo (base.html:78); flat type hierarchy + single font in transactional emails. No gradient-text, no side-stripes — clean on absolute bans except logo glow.

Browser overlay: skipped — headless browser session, no user-visible tab to present; CLI scan + manual screenshots used instead.

## Priority Issues

1. **[P1] Copy system is unprofessional** — placeholders carry instructions, labels lowercase fragments, raw enums, dev jargon, em-dash asides. NN/g: placeholders are not labels; instructions belong in persistent helper text; placeholders only for format examples. Fix: full copy pass. Labels = substantives ("Grupo do WhatsApp"), helper text below field, placeholder only for format ("financas-promo@g.us" style), map enums to display names ("WhatsApp", "Mercado Livre"), delete every "— comentário" aside, translate infra errors to user actions.
2. **[P1] Rules: no edit + duplicated settings surfaces** — editing means delete + recreate 10+ fields; Amazon credentials live in both Conta and Envios; ML connect in Conta and page of its own. One source per setting; add edit action per rule.
3. **[P1] Onboarding orphaned** — comecar.html exists, linked nowhere; new user lands on empty "Sua operação". Link checklist in nav/home until complete; redirect first login.
4. **[P1] Flagship page shows broken data as-is** — Top Promoções renders impossible offers (R$ 5,99 wine "de R$ 55,99" −89% + coupon R$ 50 OFF repeated on every row, "pendente" affiliate badges, placeholder thumbs). Undermines "Confiável". UI should suppress/flag implausible discounts and dedupe coupon noise.
5. **[P2] Send flow is blind** — Enviar modal has no message preview (templates A/B invisible at send time). Competitors sell preview as core.
6. **[P2] WCAG AA failures** — placeholder/help color #93a0b3 = 2.6:1 on white; --muted-2 dark = 3.9:1; broken QR img; "Qualquer desco" select truncation at 1440px.

## Persona Red Flags

**Jordan (first-timer, mobile)**: "12345@g.us" placeholder is the only guidance for destination when service offline; "whatsapp" lowercase select; never finds Começar. Abandons at first rule.
**Alex (semi-pro, desktop)**: cannot edit a rule; no bulk send; 12-item nav; repeated 10-field form per group. Friction at scale.
**Sam (a11y)**: placeholder contrast fail; status conveyed by dot color alone in Conexões chips; focus ring good; labeled nav good.

## Minor Observations

- KPI notes all-lowercase fragments ("sincronizada dos marketplaces") read unfinished.
- Empty tables render full header row + one centered line; use real empty state block.
- Modal Canal select raw values; badges elsewhere are styled — same data, two vocabularies.
- Native `confirm()` for delete breaks visual language.
- favicon 🛒 vs shopping-bag logo mismatch.
- home.html:40 stray `<span hidden>` mojibake — delete.
