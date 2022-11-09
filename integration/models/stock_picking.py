# See LICENSE file for full copyright and licensing details.

from odoo import models, fields
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
        for picking in self:
            picking.action_assign()
            for move in picking.move_ids.filtered(
                lambda m: m.state not in ['done', 'cancel']
            ):
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
            ctx = {
                'skip_immediate': True,
                'skip_sms': True,
                'skip_export_tracking': True,
            }
            picking.with_context(**ctx).button_validate()
        return True

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
                order._shipped_order_hook()
                order.order_export_tracking()
