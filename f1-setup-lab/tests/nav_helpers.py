"""Helpers for the dropdown navigation: links live inside closed menus until their group button is clicked."""


def open_nav(page, key):
    """Open the menu that holds the ``data-nav=key`` link (if closed) and return the link selector."""
    sel = f"#nav a[data-nav={key}]"
    if not page.is_visible(sel):  # only links inside the Historical F1 dropdown can be hidden
        page.click(f"#nav .nav-group:has(a[data-nav={key}]) .nav-head button")
        page.wait_for_selector(sel, state="visible")
    return sel


def nav_click(page, key):
    page.click(open_nav(page, key))
