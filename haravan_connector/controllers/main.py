from odoo import http
from odoo.http import request

class HaravanController(http.Controller):

    @http.route('/haravan/authentication', type='http', auth="public", website=True)
    def haravan_callback(self, **kwargs):
        code = kwargs.get('code')
        if code:
            config = request.env['haravan.config'].sudo().search([], limit=1)
            config.action_haravan_authenticate_callback(code)
            url = f"/web#id={config.id}&model=haravan.config&view_type=form"
            return request.redirect(url)

        return "Authentication Failed!"
