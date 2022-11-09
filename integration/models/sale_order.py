# See LICENSE file for full copyright and licensing details.

import logging
from collections import defaultdict

from odoo import fields, models, api, _
from odoo.tools.float_utils import float_compare
from odoo.tools import float_is_zero
from odoo.exceptions import UserError

from ...integration.exceptions import ApiImportError
from .auto_workflow.integration_workflow_pipeline import SKIP, TO_DO

_logger = logging.getLogger(__name__)


class SaleOrder(models.Model):
    _name = 'sale.order'
    _inherit = ['sale.order', 'integration.model.mixin']

    integration_id = fields.Many2one(
        string='e-Commerce Integration',
        comodel_name='sale.integration',
        readonly=True
    )

    integration_delivery_note = fields.Text(
        string='e-Commerce Delivery Note',
        copy=False,
    )

    external_sales_order_ref = fields.Char(
        string='External Sales Order Ref',
        compute='_compute_external_sales_order_ref',
        readonly=True,
        store=True,
        help='This is the reference of the Sales Order in the e-Commerce System.',
    )

    related_input_files = fields.One2many(
        string='Related input files',
        comodel_name='sale.integration.input.file',
        inverse_name='order_id',
    )

    sub_status_id = fields.Many2one(
        string='e-Commerce Order Status',
        comodel_name='sale.order.sub.status',
        domain='[("integration_id", "=", integration_id)]',
        ondelete='set null',
        copy=False,
    )

    type_api = fields.Selection(
        string='Api service',
        related='integration_id.type_api',
        help='Technical field',
    )

    payment_method_id = fields.Many2one(
        string='e-Commerce Payment method',
        comodel_name='sale.order.payment.method',
        domain='[("integration_id", "=", integration_id)]',
        ondelete='set null',
        copy=False,
    )

    integration_amount_total = fields.Monetary(
        string='e-Commerce Total Amount',
    )

    is_total_amount_difference = fields.Boolean(
        compute='_compute_is_total_amount_difference'
    )

    @property
    def integration_pipeline(self):
        pipeline = self.env['integration.workflow.pipeline'].search([
            ('order_id', '=', self.id),
        ], limit=1)
        return pipeline

    def action_cancel(self):
        res = super(SaleOrder, self).action_cancel()
        if res is True:
            self._cancel_order_hook()
        return res

    def action_integration_pipeline_form(self):
        pipeline = self.integration_pipeline
        if not pipeline:
            raise UserError(_('There is no related integration workflow pipeline.'))
        return pipeline.open_form()

    def check_is_order_shipped(self):
        """
        This method checks if the order is shipped or not.
        """
        self.ensure_one()
        is_order_shipped = False

        picking_states = [
            x for x in self.picking_ids.mapped('state') if x not in ('cancel', 'done')
        ]
        if all([
            self.state not in ('draft', 'sent', 'cancel'),
            not picking_states,
            self._is_partially_delivered(),
        ]):
            is_order_shipped = True

        return is_order_shipped

    def _is_partially_delivered(self):
        """
        Returns True if all or any lines are delivered
        :returns: boolean
        """
        self.ensure_one()
        # Skip lines with not deliverable products
        sale_lines = self.order_line.filtered(lambda rec: rec._is_deliverable_product())

        precision = self.env['decimal.precision'].precision_get('Product Unit of Measure')

        return any(
            not float_is_zero(line.qty_delivered, precision_digits=precision)
            for line in sale_lines
        )

    def write(self, vals):
        statuses_before_write = {}

        if vals.get('sub_status_id'):
            for order in self:
                statuses_before_write[order] = order.sub_status_id

        result = super().write(vals)

        if vals.get('sub_status_id'):
            for order in self:
                if statuses_before_write[order] == order.sub_status_id:
                    continue

                integration = order.integration_id
                if not integration:
                    continue

                if not integration.job_enabled('export_sale_order_status'):
                    continue

                key = f'export_sale_order_status_{order.id}'
                integration.with_context(company_id=integration.company_id.id).with_delay(
                    identity_key=key
                ).export_sale_order_status(order)

        return result

    @api.depends('amount_total', 'integration_amount_total')
    def _compute_is_total_amount_difference(self):
        for order in self:
            if not order.integration_amount_total:
                order.is_total_amount_difference = False
            else:
                order.is_total_amount_difference = float_compare(
                    value1=order.integration_amount_total,
                    value2=order.amount_total,
                    precision_digits=self.env['decimal.precision'].precision_get('Product Price'),
                ) != 0

    @api.depends('related_input_files')
    def _compute_external_sales_order_ref(self):
        for order in self:
            reference_list = order.related_input_files.mapped('order_reference')
            order.external_sales_order_ref = ', '.join(reference_list) or ''

    def _cancel_order_hook(self):
        for order in self:
            if order.integration_id.run_action_on_cancel_so:
                method_name = '_%s_cancel_order' % order.integration_id.type_api
                if hasattr(order, method_name):
                    getattr(order, method_name)()
                else:
                    _logger.warning("No method found with name '%s'" % method_name)

    def _shipped_order_hook(self):
        """
        This method is called when the order is shipped.
        """
        self.ensure_one()

        if self.integration_id.run_action_on_shipping_so:
            method_name = '_%s_shipped_order' % self.integration_id.type_api
            if hasattr(self, method_name):
                func = getattr(self, method_name)
                if callable(func):
                    func()
            else:
                _logger.warning("No method found with name '%s'" % method_name)

    def order_export_tracking(self):
        self.ensure_one()
        # only send for Done pickings that were not exported yet
        # and if this is final Outgoing picking OR dropship picking
        if self.env.context.get('skip_export_tracking'):
            return False

        integration = self.integration_id
        if not integration:
            return False

        if not integration.job_enabled('export_tracking'):
            return False

        done_pickings = self.picking_ids.filtered(
            lambda x: x.state == 'done' and not x.tracking_exported
            and (
                x.picking_type_id.id == x.picking_type_id.warehouse_id.out_type_id.id
                or ('is_dropship' in self._fields and x.is_dropship)
            )
        )

        if integration.is_carrier_tracking_required():
            done_pickings = done_pickings.filtered('carrier_tracking_ref')

        if not done_pickings:
            return False

        key = f'order_export_tracking_{self.id}'
        integration = integration.with_context(company_id=integration.company_id.id)

        return integration.with_delay(identity_key=key).export_tracking(done_pickings)

    def _run_integration_workflow(self, order_data, input_file_id=False):
        self.ensure_one()
        pipeline = self.integration_pipeline

        if not pipeline:
            _task_list, vals = self._build_task_list_and_vals(order_data)
            next_step_task_list = _task_list and (_task_list[1:] + [(False, False)])

            pipeline_task_ids = [
                (0, 0, {
                    'current_step_method': x[0],
                    'next_step_method': y[0],
                    'state': [SKIP, TO_DO][x[1]],
                })
                for x, y in zip(_task_list, next_step_task_list)
            ]
            pipeline_vals = {
                **vals,
                'order_id': self.id,
                'input_file_id': input_file_id,
                'pipeline_task_ids': pipeline_task_ids,
            }
            pipeline = self.env['integration.workflow.pipeline'].create(pipeline_vals)
            _logger.info('New pipeline for %s was created: %s', self, pipeline)

        job_kwargs = self._build_workflow_job_kwargs()
        pipeline.with_delay(**job_kwargs).trigger_pipeline()

        return pipeline

    def _build_workflow_job_kwargs(self):
        job_kwargs = {
            'channel': self.env.ref('integration.channel_sale_order').complete_name,
            'identity_key': f'integartion_workflow_pipeline-{self.integration_id.id}-{self}',
            'description': (
                f'Run Integartion Workflow: [{self.integration_id.id}] {self.display_name}'
            ),
        }
        return job_kwargs

    def _build_task_list_and_vals(self, order_data):
        integration = self.integration_id
        payment = order_data.get('payment_method')
        state_list = order_data.get('integration_workflow_states')
        PaymentExternal = self.env['integration.sale.order.payment.method.external']
        SubStatusExternal = self.env['integration.sale.order.sub.status.external']

        if not all(state_list):
            raise ApiImportError(_(
                'Current order substatus or payment not found in the parsed data:\n\n%s'
                % order_data
            ))

        payment_external = PaymentExternal\
            .get_external_by_code(integration, payment, raise_error=False)

        if payment and not payment_external:
            raise ApiImportError(
                _('Extental payment method with the code="%s" not found.' % payment)
            )

        sub_states_recordset = SubStatusExternal
        for state in state_list:
            sub_state_external = SubStatusExternal\
                .get_external_by_code(integration, state, raise_error=False)

            if not sub_state_external:
                raise ApiImportError(
                    _('Extental order substatus with the code="%s" not found.' % state)
                )
            sub_states_recordset |= sub_state_external

        pipeline_vals = {
            'payment_method_external_id': payment_external.id,
            'sub_state_external_ids': [(6, 0, sub_states_recordset.ids)],
            'force_invoice_date': any(sub_states_recordset.mapped('force_invoice_date')),
        }

        task_list = list()  # Summing of the all possible `sub-status` tasks
        for sub_state in sub_states_recordset:
            sub_task_list = sub_state.retrieve_active_workflow_tasks()
            task_list.extend(sub_task_list)

        task_dict = defaultdict(list)  # Convert tasks to a `dict` with values as `task-enable list`
        for task_name, task_enable, task_priority in task_list:
            task_dict[(task_name, task_priority)].append(task_enable)

        task_list.clear()  # Convert `task-enable list` to a `bool` value
        for (task_name, task_priority), task_enable_list in task_dict.items():
            task_list.append((task_name, any(task_enable_list), task_priority))

        task_list.sort(key=lambda x: x[2])  # Sort by `task priority`

        # [('task name', 'task enable'), ...], pipeline vals
        return [(x[0], x[1]) for x in task_list], pipeline_vals

    def _create_invoices(self, grouped=False, final=False, date=None):
        for order in self:
            if not order.env.context.get('_invoice_service_delivery'):
                continue
            for line in order.order_line:
                if line.qty_delivered_method == 'manual' and not line.qty_delivered:
                    line.write({'qty_delivered': line.product_uom_qty})
        return super(SaleOrder, self)._create_invoices(grouped=grouped, final=final, date=date)

    def _prepare_invoice(self):
        invoice_vals = super(SaleOrder, self)._prepare_invoice()
        pipeline = self.integration_pipeline

        if not pipeline:
            return invoice_vals

        if pipeline.force_invoice_date:
            invoice_date = fields.Date.context_today(self, self.date_order)
            invoice_vals['invoice_date'] = invoice_date

        if not pipeline.invoice_journal_id:
            raise UserError(_(
                'No Invoice Journal defined for Create Invoice Method. '
                'Please, define it in menu "e-Commerce Integration -> Auto-Workflow -> '
                'Order Statuses" in the "Invoice Journal" column for %s %s.'
                % (self.integration_id.name, pipeline.sub_state_external_ids.mapped('code'))
            ))

        invoice_vals['journal_id'] = pipeline.invoice_journal_id.id

        return invoice_vals

    def _integration_validate_order(self):
        _logger.info('Run integration auto-workflow validate_order')
        self.ensure_one()

        if self.state in ('sale', 'done', 'cancel'):
            return True, _('The order has been already confirmed.')

        result = self.action_confirm()
        return result, _('%s [%s] confirmed successfully.') % (self, self.display_name)

    def _integration_validate_picking(self):
        _logger.info('Run integration auto-workflow validate_picking')
        self.ensure_one()

        pickings = self.picking_ids.filtered(lambda x: x.state not in ('done', 'cancel'))
        if not pickings:
            return True, _('There are no pickings awaiting validation.')

        result = pickings._auto_validate_picking()
        return result, _('[%s] %s validated pickings successfully.') % (self, self.picking_ids)

    def _integration_create_invoice(self):  # TODO: what should we do if nothing to invoice
        _logger.info('Run integration auto-workflow create_invoice')
        self.ensure_one()

        ctx = {
            'active_ids': self.ids,
            'active_model': self._name,
        }
        advance_payment_wizard = self.env['sale.advance.payment.inv']\
            .with_context(**ctx).create({})
        result = advance_payment_wizard.\
            with_context(_invoice_service_delivery=True).create_invoices()

        if isinstance(result, dict) and result.get('type') == 'ir.actions.act_window_close':
            return True, _('[%s] %s created invoices successfully.') % (self, self.invoice_ids)
        return result, ''

    def _integration_validate_invoice(self):
        _logger.info('Run integration auto-workflow validate_invoice')
        self.ensure_one()

        invoices = self.invoice_ids.filtered(lambda x: x.state == 'draft')
        if not invoices:
            return True, _('There are no invoices awaiting validation.')

        result = invoices.with_company(self.company_id).action_post()

        if result is False:  # I don't know why, this is the Odoo standard
            return True, _('[%s] %s validated invoices successfully.') % (self, invoices)
        return result, ''

    def _integration_register_payment(self):
        _logger.info('Run integration auto-workflow register_payment')
        self.ensure_one()

        result = list()
        for invoice in self.invoice_ids:
            res = self._integration_register_payment_one(invoice)
            result.append(res)

        message = (
            _('[%s] payments for invoices %s successfully registered.') % (self, self.invoice_ids)
        )
        return all(res is True for res in result), message

    def _integration_action_cancel(self):
        _logger.info('Run integration action_cancel()')
        self.ensure_one()

        order = self.with_context(company_id=self.integration_id.company_id.id)
        result = order.action_cancel()

        message = ''
        if result is True:
            message = _('Order [%s] %s has been successfully cancelled.', order.display_name, self)
        return result, message

    def _integration_register_payment_one(self, invoice):
        if invoice.payment_state in ('paid', 'in_payment'):
            return True

        payment = self.env['account.payment'].create(
            self._prepare_integration_account_payment_dict(invoice)
        )
        payment.action_post()

        domain = [
            ('reconciled', '=', False),
            ('account_id.account_type', 'in', ('asset_receivable', 'liability_payable')),
        ]
        payment_lines = payment.line_ids.filtered_domain(domain)

        for account in payment_lines.account_id:
            (payment_lines + invoice.line_ids).filtered_domain(
                [('account_id', '=', account.id), ('reconciled', '=', False)]
            ).reconcile()

        return True

    def _prepare_integration_account_payment_dict(self, invoice):
        partner_type = (
            invoice.move_type in ('out_invoice', 'out_refund')
            and 'customer'
            or 'supplier'
        )
        payment_type = (
            invoice.move_type in ('out_invoice', 'in_refund', 'out_receipt')
            and 'inbound'
            or 'outbound'
        )

        payment_dict = {
            'amount': invoice.amount_residual,
            'partner_id': invoice.partner_id.id,
            'partner_type': partner_type,
            'payment_type': payment_type,
            'date': fields.Date.context_today(self),
            'reconciled_invoice_ids': [(6, 0, invoice.ids)],
        }

        pipeline = self.integration_pipeline
        if not pipeline or not pipeline.payment_journal_id:
            raise UserError(_(
                'No Payment Journal defined for Payment Method "%s". '
                'Please, define it in menu "e-Commerce Integration -> Auto-Workflow -> '
                'Payment Methods" in the "Payment Journal" column for %s %s.'
                % (
                    self.payment_method_id.name,
                    self.integration_id.name,
                    pipeline.payment_method_external_id.code,
                )
            ))

        payment_dict['journal_id'] = pipeline.payment_journal_id.id

        return payment_dict
