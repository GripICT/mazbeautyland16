# See LICENSE file for full copyright and licensing details.

from ..exceptions import NotMappedToExternal
from odoo.exceptions import UserError
from odoo import _


class TemplateConverter:

    def __init__(self, integration):
        self._integration = integration
        self.env = integration.env
        self._mrp_enabled = integration.is_installed_mrp

    def convert(self, template):
        Template = self.env['product.template']
        external_record = template.try_to_external_record(self._integration)
        external_id = external_record and external_record.code

        variants = template.product_variant_ids.filtered(
            lambda x: self._integration in x.integration_ids
        )

        result = {
            'id': template.id,
            'external_id': external_id,
            'type': template.type,
            'kits': self._get_kits(template),
            'products': [x.to_export_format(self._integration) for x in variants],
            'variant_count': len(variants),
        }

        search_domain = Template._template_ecommerce_field_domain(self._integration, external_id)

        for field in self.env['product.ecommerce.field.mapping'].\
                search(search_domain).mapped('ecommerce_field_id'):
            result[field.technical_name] = self._integration.calculate_field_value(template, field)

        result_upd = Template._template_converter_update(
            result,
            self._integration,
            external_record,
        )
        return result_upd

    def _get_kits(self, template):
        kits_data = []
        if not self._mrp_enabled:
            return kits_data

        kits = self.env['mrp.bom'].search([
            ('product_tmpl_id', '=', template.id),
            ('type', '=', 'phantom'),
            ('company_id', 'in', (self._integration.company_id.id, False)),
        ])

        for kit in kits:
            component_list = []

            for bom_line in kit.bom_line_ids:
                try:
                    external_record = bom_line.product_id.to_external_record(self._integration)
                except NotMappedToExternal as ex:
                    raise UserError(
                        _('Awaiting export of the "%s" product.\n%s')
                        % (bom_line.product_id.display_name, ex.args[0])
                    )

                component_list.append({
                    'qty': bom_line.product_qty,
                    'name': bom_line.display_name,
                    'product_id': external_record.code,
                    'external_reference': external_record.external_reference,
                })

            kits_data.append(dict(components=component_list))

        return kits_data
