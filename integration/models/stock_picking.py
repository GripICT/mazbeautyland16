# See LICENSE file for full copyright and licensing details.

from odoo import models, fields, _
from odoo.tools import float_compare


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    tracking_exported = fields.Boolean(
        string='Is Tracking Exported?',
        default=False,
        help='This flag allows us to define if tracking code for this picking was exported '
             'for external integration. It helps to avoid sending same tracking number twice. '
             'Basically we need this flag, cause different carriers have different type of '
             'integration. And sometimes tracking reference is added to stock picking after it '
             'is validated and not at the same moment.',
    )

    def write(self, vals):
        # if someone add `carrier_tracking_ref` after picking validation
        picking_done_ids = self.filtered(
            lambda x: x.state == 'done' and not x.carrier_tracking_ref
        )
        res = super(StockPicking, self).write(vals)

        picking_done_update_ids = picking_done_ids.filtered(
            lambda x: x.state == 'done' and x.carrier_tracking_ref
        )
        for order in picking_done_update_ids.mapped('sale_id'):
            if order.check_is_order_shipped():
                order.order_export_tracking()

        return res

    def to_export_format(self, integration):
        self.ensure_one()

        lines = []
        for move_line in self.move_ids:
            sale_line = move_line.sale_line_id
            line = {
                'id': sale_line.to_external(integration),
                'qty': move_line.quantity_done,
            }
            lines.append(line)

        result = {
            'tracking': self.carrier_tracking_ref,
            'lines': lines,
            'name': self.name,
        }

        if self.carrier_id:
            result['carrier'] = self.carrier_id.to_external(integration)

        return result

    def to_export_format_multi(self, integration):
        tracking_data = list()

        for rec in self:
            data = rec.to_export_format(integration)
            tracking_data.append(data)

        return tracking_data

    def _auto_validate_picking(self):
        """Set quantities automatically and validate the pickings."""
        ctx = {
            'skip_immediate': True,
            'skip_sms': True,
            'skip_dispatch_to_external': True,  # TODO: make it dinamically in the future
        }

        for picking in self.filtered(lambda p: p.state == 'assigned'):
            for move in picking.move_ids.filtered(
                lambda m: m.state not in ['done', 'cancel']
            ):
                if not move.reserved_availability:
                    return False, _('Not enough inventory to validate picking %s') \
                        % picking.display_name

                rounding = move.product_id.uom_id.rounding
                if (
                    float_compare(
                        move.quantity_done,
                        move.product_qty,
                        precision_rounding=rounding,
                    )
                    == -1
                ):
                    for move_line in move.move_line_ids:
                        move_line.qty_done = move_line.reserved_uom_qty

            picking.with_context(**ctx).button_validate()

            if self.filtered(lambda p: p.state == 'assigned'):
                return self._auto_validate_picking()

        if any(self.filtered(lambda p: p.state in ['waiting', 'confirmed'])):
            return False, _('[%s] is not ready to be validated') % ', '.join(
                self.filtered(lambda p: p.state == 'confirmed').mapped(
                    'display_name'
                )
            )

        return True, _('[%s] validated pickings successfully.') % ', '.join(
            self.mapped('display_name')
        )

    def button_validate(self):
        """
        Override button_validate method to called method, that check order is shipped or not.
        """
        res = super(StockPicking, self).button_validate()

        if res is not True:
            return res

        self._run_integration_picking_hooks()

        return res

    def action_cancel(self):
        res = super(StockPicking, self).action_cancel()

        if res is not True:
            return res

        self._run_integration_picking_hooks()

        return res

    def _run_integration_picking_hooks(self):
        for order in self.mapped('sale_id'):
            is_shipped = order.check_is_order_shipped()

            if is_shipped:
                order._integration_shipped_order_hook()
                order.order_export_tracking()
