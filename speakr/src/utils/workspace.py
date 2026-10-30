"""Keep authenticated HTML pages inside the shared recording workspace."""
from flask import render_template, request
from flask_login import current_user


def render_workspace_page(template_name, **context):
    embedded = request.args.get('embedded') == '1'
    if request.method == 'GET' and current_user.is_authenticated and not embedded:
        # Called only after each route's own authorization checks. The inner
        # page still uses the original route, forms, and permission checks.
        from src.api.recordings import index
        return index()
    return render_template(template_name, embedded_page=embedded, **context)
