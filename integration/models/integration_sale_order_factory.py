# See LICENSE file for full copyright and licensing details.

from ..exceptions import ApiImportError
from .sale_integration import DATETIME_FORMAT

import logging
from datetime import datetime
from dateutil import parser

from odoo import models, api, _
from odoo.exceptions import UserError
from odoo.tools.float_utils import float_compare, float_is_zero, float_round


OTHER = 'other'


_logger = logging.getLogger(__name__)


class IntegrationSaleOrderFactory(models.AbstractModel):
    _name = 'integration.sale.order.factory'
    _description = 'Integration Sale Order Factory'

    @api.model
    def create_order(self, integration, order_data):
        order = self.env['integration.sale.order.mapping'].search([
            ('integration_id', '=', integration.id),
            ('external_id.code', '=', order_data['id']),
        ]).odoo_id
        if not order:
            order = self._create_order(integration, order_data)
            order.create_mapping(integration, order_data['id'], extra_vals={'name': order.name})
            self._post_create(integration, order)
        return order

    @api.model
    def _create_order(self, integration, order_data):
        order_vals = self._prepare_order_vals(integration, order_data)

        if integration.order_name_ref:
            order_vals['name'] = '%s/%s' % (integration.order_name_ref, order_data['ref'])

        order = self.env['sale.order'].create(order_vals)

        # Configure dictionary with the default/force values after `onchange_partner_id()` method
        values = {
            'partner_invoice_id': order_vals['partner_invoice_id'],
            'partner_shipping_id': order_vals['partner_shipping_id'],
        }

        if integration.default_sales_team_id:
            values['team_id'] = integration.default_sales_team_id.id

        if integration.default_sales_person_id:
            values['user_id'] = integration.default_sales_person_id.id

        if not integration.order_name_ref:
            values['name'] = '%s/%s' % (order.name, order_data['ref'])

        delivery = self.env['res.partner'].browse(order_vals['partner_shipping_id'])

        fiscal_position = self.env['account.fiscal.position'].with_company(order.company_id)\
            ._get_fiscal_position(order.partner_id, delivery)
        values['fiscal_position_id'] = fiscal_position.id

        order.write(values)

        # 1. Creating Delivery Line
        self._create_delivery_line(integration, order, order_data)

        # 2. Creating Discount Line.
        # !!! It should be after Creating Delivery Line
        self._create_discount_line(
            integration,
            order,
            order_data.get('total_discounts_tax_incl'),
            order_data.get('total_discounts_tax_excl'),
        )

        # 3. Creating Gift Wrapping Line
        self._create_gift_line(integration, order, order_data)

        # 4. Check difference of total order amount and correct it
        #    !!! This block must be the last !!!
        if order_data.get('amount_total', False):
            price_difference = float_round(
                value=order_data['amount_total'] - order.amount_total,
                precision_digits=self.env['decimal.precision'].precision_get('Product Price'),
            )

            if price_difference:
                if price_difference > 0:
                    difference_product_id = integration.positive_price_difference_product_id
                else:
                    difference_product_id = integration.negative_price_difference_product_id

                if not difference_product_id:
                    raise ApiImportError(_('Select Price Difference Product in Sale Integration'))

                difference_line = self.env['sale.order.line'].create({
                    'product_id': difference_product_id.id,
                    'order_id': order.id,
                    'price_unit': price_difference,
                    'tax_id': False,
                })

        self._add_payment_transactions(
            order,
            integration,
            order_data.get('payment_transactions')
        )

        return order

    @api.model
    def _prepare_order_vals_hook(self, integration, original_order_data, create_order_vals):
        # Use this method to override in subclasses to define different behavior
        # of preparation of order values
        pass

    @api.model
    def _prepare_order_vals(self, integration, order_data):
        partner, shipping, billing = self._create_customer(integration, order_data)

        payment_method = self._get_payment_method(integration, order_data['payment_method'])

        delivery_notes_field_name = integration.so_delivery_note_field.name
        delivery_notes_value = order_data['delivery_notes'] or ''

        if order_data.get('gift_wrapping') and order_data.get('gift_message'):
            delivery_notes_value += _('\nMessage to write: %s') % order_data.get('gift_message')

        amount_total = order_data.get('amount_total', False)

        order_line = []
        for line in order_data['lines']:
            order_line.append((0, 0, self._prepare_order_line_vals(integration, line)))

        order_vals = {
            'integration_id': integration.id,
            'integration_amount_total': amount_total,
            'partner_id': partner.id if partner else False,
            'partner_shipping_id': shipping.id if shipping else False,
            'partner_invoice_id': billing.id if billing else False,
            'order_line': order_line,
            'payment_method_id': payment_method.id,
            delivery_notes_field_name: delivery_notes_value,
        }

        if integration.so_external_reference_field:
            order_vals[integration.so_external_reference_field.name] = order_data['ref']

        if order_data.get('date_order'):
            date_order = order_data['date_order']
            data_converted = parser.isoparse(date_order)
            order_vals['date_order'] = datetime.strftime(data_converted, DATETIME_FORMAT)

        current_state = order_data.get('current_order_state')
        if current_state:
            sub_status = self._get_order_sub_status(
                integration,
                current_state,
            )
            order_vals['sub_status_id'] = sub_status.id

        pricelist = self._get_order_pricelist(integration, order_data)
        if pricelist:
            order_vals['pricelist_id'] = pricelist.id

        self._prepare_order_vals_hook(integration, order_data, order_vals)

        return order_vals

    @api.model
    def _get_order_sub_status(self, integration, ext_current_state):
        SubStatus = self.env['sale.order.sub.status']

        sub_status = SubStatus.from_external(
            integration, ext_current_state, raise_error=False)

        if not sub_status:
            integration.integrationApiImportSaleOrderStatuses()

            sub_status = SubStatus.from_external(
                integration, ext_current_state)

        return sub_status

    def _get_order_pricelist(self, integration, order_data):
        company = integration.company_id
        company_currency_iso = company.currency_id.name
        ecommerce_currency_iso = order_data.get('currency', '')

        if not all([company_currency_iso, ecommerce_currency_iso]):
            return False

        if company_currency_iso.lower() == ecommerce_currency_iso.lower():
            return False

        odoo_currency = self.env['res.currency'].search([
            ('name', '=ilike', ecommerce_currency_iso.lower()),
        ], limit=1)
        if not odoo_currency:
            raise ApiImportError(_(
                'Currency ISO code "%s" was not found in Odoo.' % ecommerce_currency_iso
            ))

        Pricelist = self.env['product.pricelist']

        pricelists = Pricelist.search([
            ('company_id', 'in', (company.id, False)),
            ('currency_id', '=', odoo_currency.id),
        ])
        pricelist = pricelists.filtered(lambda x: x.company_id == company)[:1] or pricelists[:1]

        if not pricelist:
            vals = {
                'company_id': company.id,
                'currency_id': odoo_currency.id,
                'name': f'Integration {ecommerce_currency_iso}',
            }
            pricelist = Pricelist.create(vals)

        return pricelist

    @api.model
    def _create_customer(self, integration, order_data):
        customer = False
        shipping = False
        billing = False

        if order_data.get('customer'):
            customer = self._fetch_odoo_partner(
                integration,
                order_data['customer'],
            )

        if order_data.get('shipping'):
            shipping = self._fetch_odoo_partner(
                integration,
                order_data['shipping'],
                OTHER,
                customer.id if customer else False,
            )

        if order_data.get('billing'):
            billing = self._fetch_odoo_partner(
                integration,
                order_data['billing'],
                OTHER,
                customer.id if customer else False,
            )

        return self._prepare_so_contacts(integration, customer, shipping, billing)

    @api.model
    def _prepare_so_contacts(self, integration, customer, shipping, billing):
        if not customer:
            if not integration.default_customer:
                raise ApiImportError(_('Default Customer is empty. Please, feel it in '
                                       'Sale Integration on the tab "Sale Order Defaults"'))

            customer = integration.default_customer

        if not shipping:
            shipping = integration.default_customer

        if not billing:
            billing = integration.default_customer

        return customer, shipping, billing

    @api.model
    def _find_odoo_country(self, integration, partner_data):
        country = self.env['res.country']
        if partner_data.get('country'):
            country = self.env['res.country'].from_external(
                integration, partner_data.get('country')
            )
        elif partner_data.get('country_code'):
            country = self.env['res.country'].search([
                ('code', '=ilike', partner_data.get('country_code')),
            ], limit=1)
        return country

    @api.model
    def _find_odoo_state(self, integration, odoo_country, partner_data):
        state = self.env['res.country.state']
        if not state.search([('country_id', '=', odoo_country.id)]):
            # If it is a Country without known states in Odoo let's skip this `finding`
            return state

        if partner_data.get('state'):
            state = state.from_external(
                integration,
                partner_data.get('state'),
            )
        elif partner_data.get('state_code') and odoo_country:
            state = state.search([
                ('country_id', '=', odoo_country.id),
                ('code', '=ilike', partner_data.get('state_code')),
            ], limit=1)

        return state

    @api.model
    def _create_or_update_odoo_partner(self,
                                       integration,
                                       ext_partner_code,
                                       partner_vals,
                                       parent_id=False):
        partner = None
        # If there is external code specified for Partner, then we first try to search
        # by external code
        if ext_partner_code:
            partner = self.env['res.partner'].from_external(
                integration,
                ext_partner_code,
                raise_error=False,
            )

        if partner:
            partner_vals.pop('type', False)
            partner.write(partner_vals)
            return partner

        # If no partner found, try to search by more complex criteria
        # So if we found exact match then we want to associate this partner
        # with external partner
        search_criteria = []
        if partner_vals.get('email'):
            search_criteria.append(('email', '='))
        elif partner_vals.get('phone'):
            search_criteria.append(('phone', '='))

        search_criteria += [
            ('name', '=ilike'),
            ('street', '=ilike'),
            ('street2', '=ilike'),
            ('city', '=ilike'),
            ('zip', '=ilike'),
            ('state_id', '='),
            ('country_id', '='),
        ]
        domain = [
            (key, op if partner_vals.get(key, False) else 'in',
                partner_vals.get(key, ['', False])) for key, op in search_criteria
        ]

        partner = self.env['res.partner'].search(domain)

        if partner:
            if parent_id:
                filter_partner = partner.filtered(lambda x: x.parent_id.id == parent_id)
                partner = filter_partner or partner

            if 'type' in partner_vals:
                filter_partner = partner.filtered(lambda x: x.type == partner_vals['type'])
                partner = filter_partner or partner

            partner = partner[:1]

        # We need this because in OCA module partner_firstname removes 'name' from vals
        partner_name = partner_vals['name']

        if partner:
            # After search if found, update with new values,
            # But we need to update ONLY if this partner has external code
            # If not, it doesn't make sense to update it because it is some existing partner
            if ext_partner_code:
                partner.write(partner_vals)
        else:
            # We should set parent company ONLY for partners that are newly created partners
            # To avoid breaking existing partners who maybe already linked to some another
            # parent partner. Also, we allow to switch on and off linking of parent based
            # on the integration
            if parent_id and integration._should_link_parent_contact():
                partner_vals['parent_id'] = parent_id

            # This context is needed so partner will be created as customer
            # So if we haven't defined exact type - this is the parent customer
            # And it should be marked as customer (visible in Customer menu)
            ctx = dict()
            if 'type' not in partner_vals:
                ctx['res_partner_search_mode'] = 'customer'

            partner = self.env['res.partner'].with_context(**ctx).create(partner_vals)

        # And finally create mapping in case of existing external code
        # Because if we are here, previously we were not able to find partner
        # by its mapping in external tables, so need to create one
        if ext_partner_code:
            partner.create_mapping(
                integration,
                ext_partner_code,
                extra_vals={'name': partner_name},
            )

        return partner

    @api.model
    def _fetch_odoo_partner(self, integration, partner_data, address_type=None, parent_id=False):

        partner_vals = {
            'name': partner_data['person_name'],
            'integration_id': integration.id,
        }

        country = self._find_odoo_country(integration, partner_data)
        if country:
            partner_vals['country_id'] = country.id

        state = self._find_odoo_state(integration, country, partner_data)
        if state:
            partner_vals['state_id'] = state.id

        for key in ['street', 'street2', 'city', 'zip', 'email', 'phone', 'mobile']:
            if partner_data.get(key):
                partner_vals[key] = partner_data.get(key)

        if partner_data.get('language'):
            language = self.env['res.lang'].from_external(
                integration, partner_data.get('language')
            )
            if language:
                partner_vals['lang'] = language.code

        person_id_field = integration.customer_personal_id_field
        if person_id_field:
            partner_vals[person_id_field.name] = partner_data.get('person_id_number')

        if address_type:
            partner_vals['type'] = address_type

        # Adding Company Specific fields
        if partner_data.get('company_name'):
            partner_vals['company_name'] = partner_data.get('company_name')

        company_vat_field = integration.customer_company_vat_field
        if company_vat_field and partner_data.get('company_reg_number'):
            partner_vals[company_vat_field.name] = partner_data.get('company_reg_number')

        partner = self._create_or_update_odoo_partner(
            integration,
            partner_data.get('id'),
            partner_vals,
            parent_id,
        )
        return partner

    @api.model
    def _get_odoo_product(self, integration, variant_code, raise_error):
        product = self.env['product.product'].from_external(
            integration,
            variant_code,
            raise_error=False,
        )

        if not product and raise_error:
            raise ApiImportError(_(
                'Failed to find external variant with code "%s". Please, run "IMPORT PRODUCT '
                'FROM EXTERNAL" using button on "Initial Import" tab on your sales integration '
                'with name "%s". After this make sure that all your products are mapped '
                'in "Mappings - Products" and "Mappings - '
                'Variants" menus.') % (variant_code, integration.name)
            )

        return product

    @api.model
    def _try_get_odoo_product(self, integration, line):
        variant_code = line['product_id']
        product = self._get_odoo_product(integration, variant_code, False)

        if product:
            return product

        # Looks like this is new product in e-Commerce system
        # Or it is not fully mapped. In any case let's try to repeat mapping
        # for only this product and then try to find it again
        # If not found in this case, raise error
        template_code = variant_code.split('-')[0]
        integration.import_external_product(template_code)

        product = self._get_odoo_product(integration, variant_code, True)

        return product

    @api.model
    def _prepare_order_line_vals(self, integration, line):
        product = self._try_get_odoo_product(integration, line)

        vals = {
            'product_id': product.id,
            'integration_external_id': line['id'],
        }

        if 'product_uom_qty' in line:
            vals.update(product_uom_qty=line['product_uom_qty'])

        taxes = self.get_taxes_from_external_list(integration, line['taxes'])
        vals['tax_id'] = [(6, 0, taxes.ids)]

        if taxes and self._get_tax_price_included(taxes):
            if 'price_unit_tax_incl' in line:
                vals.update(price_unit=line['price_unit_tax_incl'])
        else:
            if 'price_unit' in line:
                vals.update(price_unit=line['price_unit'])

        if 'discount' in line:
            vals.update(discount=line['discount'])

        return vals

    def get_taxes_from_external_list(self, integration, external_tax_ids):
        taxes = self.env['account.tax']

        for external_tax_id in external_tax_ids:
            taxes |= self.try_get_odoo_tax(integration, external_tax_id)

        return taxes

    def try_get_odoo_tax(self, integration, tax_id):
        tax = tax = self.env['account.tax'].from_external(
            integration,
            tax_id,
            raise_error=False,
        )

        if tax:
            return tax

        tax = integration._import_external_tax(tax_id)

        if not tax:
            raise ApiImportError(_(
                'Failed to find external tax with code "%s". Please, run "IMPORT MASTER DATA" '
                'using button on "Initial Import" tab on your sales integration "%s". '
                'After this make sure that all your delivery carrier are mapped '
                'in "Mappings - Taxes" menus.') % (tax_id, integration.name)
            )

        return tax

    @api.model
    def _post_create(self, integration, order):
        pass

    @api.model
    def _get_tax_price_included(self, taxes):
        price_include = all(tax.price_include for tax in taxes)

        if not price_include and any(tax.price_include for tax in taxes):
            raise ApiImportError(_('One line has different Included In Price parameter in Taxes'))

        # If True - the price includes taxes
        return price_include

    def try_get_odoo_delivery_carrier(self, integration, carrier_data):
        code = carrier_data['id']
        carrier = self.env['delivery.carrier'].from_external(
            integration,
            code,
            raise_error=False,
        )
        if carrier:
            return carrier

        carrier = integration._import_external_carrier(carrier_data)

        if not carrier:
            raise ApiImportError(_(
                'Failed to find external carrier with code "%s". Please, run "IMPORT MASTER DATA" '
                'using button on "Initial Import" tab on your sales integration "%s". '
                'After this make sure that all your delivery carrier are mapped '
                'in "Mappings - Shipping Methods" menus.') % (code, integration.name)
            )

        return carrier

    def _create_delivery_line(self, integration, order, order_data):
        if order_data['carrier'] and order_data['carrier'].get('id'):
            carrier = self.try_get_odoo_delivery_carrier(integration, order_data['carrier'])
            order.set_delivery_line(carrier, order_data['shipping_cost'])

            delivery_line = order.order_line.filtered(lambda line: line.is_delivery)

            if not delivery_line:
                return

            taxes = self.get_taxes_from_external_list(
                integration,
                order_data.get('carrier_tax_ids', []),
            )
            delivery_line.tax_id = [(6, 0, taxes.ids)]

            if order_data.get('carrier_tax_rate') == 0:
                if not all(x.amount == 0 for x in delivery_line.tax_id):
                    delivery_line.tax_id = False

            if 'shipping_cost_tax_excl' in order_data:
                if not self._get_tax_price_included(delivery_line.tax_id):
                    delivery_line.price_unit = order_data['shipping_cost_tax_excl']

    def _create_gift_line(self, integration, order, order_data):
        if order_data.get('gift_wrapping'):
            if not integration.gift_wrapping_product_id:
                raise ApiImportError(_('Gift Wrapping Product is empty. Please, feel it in '
                                       'Sale Integration on the tab "Sale Order Defaults"'))

            gift_taxes = self.get_taxes_from_external_list(
                integration,
                order_data.get('wrapping_tax_ids', []),
            )

            if self._get_tax_price_included(gift_taxes):
                gift_price = order_data.get('total_wrapping_tax_incl', 0)
            else:
                gift_price = order_data.get('total_wrapping_tax_excl', 0)

            gift_line = self.env['sale.order.line'].create({
                'product_id': integration.gift_wrapping_product_id.id,
                'order_id': order.id,
                'tax_id': gift_taxes.ids,
                'price_unit': gift_price,
            })

            if order_data.get('gift_message'):
                gift_line.name += _('\nMessage to write: %s') % order_data.get('gift_message')

    def _insert_line_in_order(self, integration, order, price_unit, tax_id):
        line = self.env['sale.order.line'].create({
            'product_id': integration.discount_product_id.id,
            'order_id': order.id,
            'price_unit': price_unit,
            'tax_id': tax_id and tax_id.ids or False,
        })
        return line

    def _create_discount_line(self, integration, order, discount_tax_incl, discount_tax_excl):
        if not discount_tax_incl or not discount_tax_excl:
            return

        if not integration.discount_product_id:
            raise ApiImportError(_('Discount Product is empty. Please, feel it in '
                                   'Sale Integration on the tab "Sale Order Defaults"'))

        precision = self.env['decimal.precision'].precision_get('Product Price')

        product_lines = order.order_line.filtered(lambda x: not x.is_delivery)

        # Taxes must be with '-'
        discount_taxes = discount_tax_excl - discount_tax_incl

        if self._get_tax_price_included(product_lines.mapped('tax_id')):
            discount_price = discount_tax_incl * -1
        else:
            discount_price = discount_tax_excl * -1

        discount_line = self._insert_line_in_order(integration, order, discount_price, False)

        # 1. Discount without taxes
        if float_is_zero(discount_taxes, precision_digits=precision):
            return

        # 2. Try to find the most suitable tax.
        #  Basically it's made for PrestaShop because it gives only discount with/without taxes
        #  We try to understand whether discount applied to all lines, one line
        #  or lines with identical taxes by the minimal calculated tax difference.
        #  Otherwise we apply discount to all lines
        #  TODO For Other shops we should make with taxes from discount in order data

        # 2.1 Group lines by taxes
        all_grouped_taxes = {}
        grouped_taxes = {}
        line_taxes = {}
        all_lines_sum = 0

        for line in product_lines:
            tax_key = str(line.tax_id)
            line_key = str(line.id)
            all_lines_sum += line.price_subtotal

            grouped_taxes.update({tax_key: {
                'tax_id': line.tax_id,
                'discount': discount_price,
            }})
            line_taxes.update({line_key: {
                'tax_id': line.tax_id,
                'discount': discount_price,
            }})
            all_grouped_taxes.update({tax_key: {
                'price_subtotal': (line.price_subtotal +
                                   all_grouped_taxes.get(tax_key, {}).get('price_subtotal', 0)),
                'tax_id': line.tax_id,
            }})

        # 2.2 Distribution of the amount to different tax groups
        all_grouped_taxes = [grouped_tax for grouped_tax in all_grouped_taxes.values()]
        residual_amount = discount_price
        line_num = len(all_grouped_taxes)

        for tax_value in all_grouped_taxes:
            if line_num == 1:
                tax_value['discount'] = residual_amount
            else:
                tax_value['discount'] = float_round(
                    value=discount_price * tax_value['price_subtotal'] / all_lines_sum,
                    precision_digits=precision
                )

            residual_amount -= tax_value['discount']
            line_num -= 1

        # 2.3 Calculate tax difference for different combinations
        def calc_tax_summa(tax_values):
            tax_amount = 0

            for tax_value in tax_values:
                discount_line.tax_id = tax_value['tax_id']
                discount_line.price_unit = tax_value['discount']
                tax_amount += discount_line.price_tax

            return {
                'grouped_taxes': tax_values,
                'tax_diff': abs(tax_amount - discount_taxes),
            }

        # discount taxes for all
        calc_taxes = [calc_tax_summa(all_grouped_taxes)]
        # discount taxes one by one for tax groups
        calc_taxes += [calc_tax_summa([grouped_tax]) for grouped_tax in grouped_taxes.values()]
        # discount taxes one by one for line
        calc_taxes += [calc_tax_summa([line_tax]) for line_tax in line_taxes.values()]

        # 2.4 Get tax with MINIMAL difference
        # If price difference > 1% then apply discount to all taxes
        calc_taxes.sort(key=lambda calc_tax: calc_tax['tax_diff'])

        if abs(calc_taxes[0]['tax_diff'] / discount_taxes) < 0.01:
            the_most_suitable_discount = calc_taxes[0]['grouped_taxes']
        else:
            the_most_suitable_discount = all_grouped_taxes

        # Delete old delivery line
        discount_line.unlink()

        discount_lines = self.env['sale.order.line']

        # 2.5 Create discount lines for discount
        for tax_value in the_most_suitable_discount:
            discount_lines += self._insert_line_in_order(
                integration,
                order,
                tax_value['discount'],
                tax_value['tax_id']
            )

    def _add_payment_transactions(self, order, integration, payment_transactions):
        if not payment_transactions or not integration.import_payments:
            return
        # In Odoo standard it is not possible to add payments to sales order
        # So we are checking if special field exists for this
        # for now we allow to work with this OCA module
        # https://github.com/OCA/sale-workflow/tree/16.0/sale_advance_payment
        # TODO: Integrate functionality in integration module
        if 'account_payment_ids' not in self.env['sale.order']._fields:
            return

        precision = self.env['decimal.precision'].precision_get('Product Price')
        for transaction in payment_transactions:
            # we skip zero transaction as they doesn't make sense
            if float_is_zero(transaction['amount'], precision_digits=precision):
                _logger.warning(_('Order % was received with payment amount equal to 0.0. '
                                  'Skipping payment attachment to the order') % order.name)
                continue
            if not transaction['transaction_id']:
                _logger.warning(_('Order % payment doesn\'t have transaction id specified.'
                                  ' Skipping payment attachment to the order') % order.name)
                continue

            # Currency is not required field for transaction,
            # so we calculate it either from pricelist
            # or from company related to SO
            # And then try to get it from received SO
            currency_id = order.pricelist_id.currency_id.id or order.company_id.currency_id.id
            if transaction.get('currency'):
                odoo_currency = self.env['res.currency'].search([
                    ('name', '=ilike', transaction['currency'].lower()),
                ], limit=1)
                if not odoo_currency:
                    raise ApiImportError(
                        _('Currency ISO code "%s" was not found in Odoo.') % transaction['currency']
                    )
                currency_id = odoo_currency.id

            # Get Payment journal based on the payment method
            external_payment_method = order.payment_method_id.to_external_record(integration)

            if not external_payment_method.payment_journal_id:
                raise UserError(
                    _('No Payment Journal defined for Payment Method "%s". '
                      'Please, define it in menu "e-Commerce Integration -> Auto-Workflow -> '
                      'Payment Methods" in the "Payment Journal" column')
                    % order.payment_method_id.name
                )
            payment_vals = {
                "date": transaction['transaction_date'],
                "amount": abs(transaction['amount']),  # can be negative, so taking absolute value
                "payment_type": 'inbound' if transaction['amount'] > 0.0 else 'outbound',
                "partner_type": "customer",
                "ref": transaction['transaction_id'],
                "journal_id": external_payment_method.payment_journal_id.id,
                "currency_id": currency_id,
                "partner_id": order.partner_invoice_id.commercial_partner_id.id,
                "payment_method_id": self.env.ref(
                    "account.account_payment_method_manual_in"  # TODO: set smth else
                ).id,
            }

            payment_obj = self.env["account.payment"]
            payment = payment_obj.create(payment_vals)
            order.account_payment_ids |= payment
            payment.action_post()

    @api.model
    def _get_payment_method(self, integration, ext_payment_method):
        PaymentMethod = self.env['sale.order.payment.method']

        payment_method = PaymentMethod.from_external(
            integration, ext_payment_method, raise_error=False)

        if not payment_method:
            payment_method = PaymentMethod.search([
                ('name', '=', ext_payment_method),
                ('integration_id', '=', integration.id),
            ])

            if not payment_method:
                payment_method = PaymentMethod.create({
                    'name': ext_payment_method,
                    'integration_id': integration.id,
                })
            extra_vals = {'name': ext_payment_method}
            self.env['integration.sale.order.payment.method.mapping'].create_integration_mapping(
                integration, payment_method, ext_payment_method, extra_vals)

        return payment_method
