# See LICENSE file for full copyright and licensing details.

import logging
from collections import defaultdict
import base64
import re

from odoo import fields, models, api, _
from odoo.tools.float_utils import float_compare
from odoo.tools import float_is_zero
from odoo.exceptions import UserError, ValidationError

from ...integration.exceptions import ApiImportError
from .auto_workflow.integration_workflow_pipeline import SKIP, TO_DO
from .external.integration_sale_order_payment_method_external import INV_VALIDATED

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
            for order in self:
                order._integration_cancel_order_hook()
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

        if self.env.context.get('skip_dispatch_to_external'):
            return result

        if vals.get('sub_status_id'):
            for order in self:
                if statuses_before_write[order] == order.sub_status_id:
                    continue

                integration = order.integration_id
                if not integration:
                    continue

                if not integration.job_enabled('export_sale_order_status'):
                    continue

                job_kwargs = {
                    'identity_key': f'export_sale_order_status_{order.id}',
                    'description': (
                        f'Export Sale Order [{order.id}] sub-status: {order.sub_status_id.name}'
                    ),
                }
                integration.with_context(company_id=integration.company_id.id)\
                    .with_delay(**job_kwargs).export_sale_order_status(order)

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

    def _integration_cancel_order_hook(self):
        self.ensure_one()
        result = None
        if not self.integration_id:
            return result

        if self.integration_id.run_action_on_cancel_so:
            result = self._perform_method_by_name(f'_{self.type_api}_cancel_order')
        return result

    def _integration_shipped_order_hook(self):
        self.ensure_one()
        result = None
        if not self.integration_id:
            return result

        if self.integration_id.run_action_on_shipping_so:
            result = self._perform_method_by_name(f'_{self.type_api}_shipped_order')
        return result

    def _integration_validate_invoice_order_hook(self):
        self.ensure_one()
        result = None
        if not self.integration_id:
            return result

        invoice_full_validated = all(x.state == 'posted' for x in self.invoice_ids)
        payment_method_external = self.payment_method_id.to_external_record(self.integration_id)
        action_after_validation = payment_method_external.send_payment_status_when == INV_VALIDATED

        if invoice_full_validated and action_after_validation:
            _logger.info(
                '%s: force export paid status for %s to external.', self.integration_id.name, self
            )
            result = self.with_context(force_export_paid_status=True)._integration_paid_order_hook()
        return result

    def _integration_paid_order_hook(self):
        self.ensure_one()
        result = None
        if not self.integration_id:
            return result

        if self.integration_id.run_action_on_so_invoice_status:
            invoice_full_paid = all(
                x.payment_state in ('paid', 'in_payment') for x in self.invoice_ids
            )
            action_after_paid = invoice_full_paid or self._context.get('force_export_paid_status')

            if self.invoice_status == 'invoiced' and action_after_paid:
                result = self._perform_method_by_name(f'_{self.type_api}_paid_order')
        return result

    def order_export_tracking(self):
        self.ensure_one()
        # only send for Done pickings that were not exported yet
        # and if this is final Outgoing picking OR dropship picking
        if self.env.context.get('skip_dispatch_to_external'):
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

    def _prepare_vals_for_sale_order_status(self):
        integration = self.integration_id
        order_id = self.to_external(integration)
        status = self.sub_status_id.to_external(integration)
        return {
            'order_id': order_id,
            'status': status,
        }

    def _build_and_run_integration_workflow(self, order_data, input_file_id=False):
        self.ensure_one()
        _logger.info('Create new (or use existing) %s pipeline', self)
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
        if self.env.context.get('from_integration_workflow'):
            for order in self:
                for line in order.order_line:
                    if line.qty_delivered_method == 'manual' and not line.qty_delivered:
                        line.write({'qty_delivered': line.product_uom_qty})

        return super(SaleOrder, self)._create_invoices(grouped=grouped, final=final, date=date)

    def _prepare_invoice(self):
        invoice_vals = super(SaleOrder, self)._prepare_invoice()

        if self.env.context.get('from_integration_workflow'):
            pipeline = self.integration_pipeline

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
            return True, _('[%s] There are no pickings awaiting validation.') % self.display_name

        result, message = pickings._auto_validate_picking()
        return result, '[%s] %s' % (self.display_name, message)

    def _integration_create_invoice(self):  # TODO: what should we do if nothing to invoice
        _logger.info('Run integration auto-workflow create_invoice')
        self.ensure_one()

        result = self.with_context(from_integration_workflow=True)._create_invoices(final=True)

        message = _('[%s] %s created invoices successfully.') % (self, result) if result else ''
        return bool(result), message

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

    def _integration_action_cancel_no_dispatch(self):
        ctx = dict(skip_dispatch_to_external=True)
        return self.with_context(**ctx)._integration_action_cancel()

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

    def _prepare_confirmation_values(self):
        res = super()._prepare_confirmation_values()
        if self.integration_id:
            res.update({
                'date_order': self.date_order,
            })
        return res

    def _prepare_pdf_invoice(self):
        self.ensure_one()

        code = 0
        message = ''
        data = []

        if self.state not in ('sale', 'done'):
            code, message = 1, f'Order {self.display_name} is not confirmed yet.'
            return code, message, data

        if not self.invoice_ids:
            if self.invoice_status != 'to invoice':
                code = 1
                message = 'Order %s has no invoices and hasn\'t status "to invoice".' \
                          % self.display_name
                return code, message, data

        # Create invoice if it is not created yet
        if self.invoice_status == 'to invoice':
            try:
                invoice_created = self._create_invoices(final=True)
                if not invoice_created:
                    code, message = 1, 'Invoice creation error'
                    return code, message, data

            except (ValidationError, UserError) as e:
                code, message = 1, e.args[0]
                return code, message, data

            except Exception as e:
                code, message = 1, e.args[0]
                return code, message, data

        # Validate invoice if it is not validated yet
        try:
            invoice_validated, _ = self._integration_validate_invoice()
            if not invoice_validated:
                code, message = 1, 'Invoice validation error'
                return code, message, data

        except (ValidationError, UserError) as e:
            code, message = 1, e.args[0]
            return code, message, data

        except Exception as e:
            code, message = 1, e.args[0]
            return code, message, data

        for invoice in self.invoice_ids:
            report_template = self.env.ref('account.account_invoices')
            invoice_pdf = report_template._render_qweb_pdf(invoice.id)[0]
            invoice_pdf_name = re.sub(r'\W+', '', invoice.name) + '.pdf'

            access_token = self.env['ir.attachment']._generate_access_token()
            attachment = self.env['ir.attachment'].create({
                'name': invoice_pdf_name,
                'type': 'binary',
                'datas': base64.b64encode(invoice_pdf),
                'res_model': 'sale.order',
                'res_id': self.id,
                'mimetype': 'application/x-pdf',
                'access_token': access_token,
            })

            base_url = self.env['ir.config_parameter'].get_param('web.base.url')
            url_open = f'%s/web/content/%s?download=True&access_token=%s' \
                       % (base_url, attachment.id, access_token)
            data.append({
                'name': invoice_pdf_name,
                'link': url_open,
            })
            message = 'Link to invoice PDF file has been successfully generated.'

        return code, message, data
