# -*- coding: utf-8 -*-
import json
import logging

from odoo import fields, http
from odoo.http import request, Response
_logger = logging.getLogger(__name__)

class HaravanWebhookController(http.Controller):

    @http.route(
        '/haravan/webhook/<int:config_id>',
        type='http',
        auth='public',
        methods=['POST', 'GET'],
        csrf=False,
    )
    def receive_webhook(self, config_id, **kwargs):
        """Entry point for all Haravan webhook POST requests."""
        if request.httprequest.method == 'GET':
            challenge = request.params.get('hub.challenge')
            verify_token = request.params.get('hub.verify_token')

            if challenge:
                return challenge
            return "No challenge provided", 403

        raw_body = request.httprequest.get_data()
        signature = request.httprequest.headers.get('X-Haravan-Hmacsha256', '')
        topic = request.httprequest.headers.get('X-Haravan-Topic', '')

        if not topic:
            _logger.warning('Webhook missing X-Haravan-Topic header')
            return Response('Bad Request', status=400)

        config = request.env['haravan.config'].sudo().browse(config_id).exists()
        if not config:
            _logger.warning('Webhook received for unknown config_id=%s', config_id)
            return Response('Not Found', status=404)
        if config.webhook_secret and not config.verify_webhook_signature(raw_body, signature):
            _logger.warning('Webhook HMAC mismatch for config %s topic %s', config_id, topic)
            return Response('Unauthorized', status=401)

        handler_key = topic.replace('/', '_')
        webhook = request.env['haravan.webhook'].sudo().search(
            [('config_id', '=', config_id), ('topic', '=', topic)], limit=1
        )
        if webhook:
            webhook.sudo().write({
                'last_triggered': fields.Datetime.now(), 
                'trigger_count': webhook.trigger_count + 1,
            })
        else:
            try:
                request.env['haravan.webhook'].sudo().create({
                    'config_id': config_id,
                    'topic': topic,
                    'last_triggered': fields.Datetime.now(),
                    'trigger_count': 1,
                })
            except Exception as e:
                _logger.warning('Could not auto-create webhook for topic %s: %s', topic, e)

        if not raw_body:
            return Response('Empty body', status=400)
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as e:
            _logger.warning('Webhook JSON parse error [%s/%s]: %s', config_id, topic, e)
            return Response('Bad Request', status=400)

        if not isinstance(payload, dict):
            _logger.warning('Webhook payload is not a dict [%s/%s]', config_id, topic)
            return Response('Bad Request', status=400)

        # Offload processing to queue_job
        request.env['haravan.webhook'].sudo().with_delay().process_webhook(config_id, topic, payload)

        return Response('OK', status=200)
