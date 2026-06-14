- **Design Philosophy:** Always design for mobile screens first (smallest viewport), then use responsive breakpoints (e.g., Tailwind's `md:`, `lg:`) to scale up for desktop. Never do the inverse.
- **Layouts:** Use Flexbox (`flex flex-col`) or CSS Grid (`grid grid-cols-1`) as the default for mobile. Switch to horizontal layouts (`flex-row` or `grid-cols-2+`) only at larger breakpoints (`md:` or higher).
- **Touch Targets:** Ensure all interactive elements (buttons, links, inputs) have a minimum touch target size of 44x44px on mobile to avoid misclicks. Add generous padding.
- **Typography & Spacing:** Use fluid typography and relative spacing (`rem`, `em`, or Tailwind's spacing scale). Keep margins and paddings tighter on mobile (e.g., `p-4`) and expand them on desktop (e.g., `md:p-8`).
- **Navigation:** Navigation bars must collapse into a functional mobile menu (hamburger/drawer or a clean bottom-bar navigation) on small screens. Do not let desktop menus overflow or wrap awkwardly.
- **Django Integration:** Ensure that loops (e.g., `{% for item in items %}`) render elements in a responsive grid/list that wraps cleanly on mobile devices without horizontal scrolling.
- **Mandatory Check:** After generating or significantly modifying any Django template, you MUST use the Playwright MCP server to visually inspect the result before finalizing the task.
- **Environment Setup:** Ensure the Django local server is running (`python manage.py runserver`). If it is not active, use your terminal tools to start it.
- **Responsiveness Validation Workflow:**
  1. **Mobile First:** Launch a Playwright browser instance emulating a mobile viewport (e.g., iPhone 14 dimensions: 390x844). Navigate to the modified page.
  2. Take a screenshot and analyze the layout. Check for horizontal scrolling, text wrapping issues, overlapping elements, or tiny touch targets.
  3. **Desktop Scale:** Resize the viewport to desktop dimensions (e.g., 1440x900). Take another screenshot to verify that the Tailwind layout scaled up correctly.
- **Correction Loop:** If any visual anomaly, broken grid, alignment issue, or unstyled element is detected in the screenshots, immediately fix the HTML/Tailwind classes and repeat the Playwright validation until it looks flawless.
- **UX/UI Research:** Whenever the user asks for examples, ideas, patterns, or "good uses" related to UX or UI, FIRST research online (WebSearch) for current best practices and real-world examples before implementing. Cite the patterns/sources you based the decision on. Do not rely solely on memory for design decisions.
## Security Audit & Pentesting Rules
- **Pre-Deployment Audit:** Before marks any user authentication, payment flow, or data-handling feature as "done", you MUST run a security evaluation using pentest tools.
- **Form & Input Testing:** Use the pentest/Playwright tools to fuzz input fields and URL parameters. Explicitly test for SQL Injection in Django QuerySets and XSS in templates (verify that Django's auto-escaping `{{ value }}` hasn't been bypassed with `|safe` accidentally).
- **Endpoint Protection:** Verify that all sensitive routes have proper decorator checks (e.g., `@login_required`, `PermissionRequiredMixin`) and that HTMX endpoints do not leak internal data if called directly without the proper headers.
- **Reporting:** If any vulnerability is found, stop development immediately, document the vulnerability vector, patch the Django code, and re-run the scan to confirm the fix.