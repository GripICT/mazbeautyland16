# See LICENSE file for full copyright and licensing details.

from __future__ import absolute_import

from .abstract_apiclient import AbsApiClient


class NoAPIClient(AbsApiClient):
    settings_fields = ()

    def __init__(self, settings):
        super(NoAPIClient, self).__init__(settings)

    def receiveOrders(self, settings):
        raise NotImplementedError()

    def parseOrder(self, settings, raw_order):
        raise NotImplementedError()

    def acknowledgementOrder(self, order):
        raise NotImplementedError()

    def createOrUpdateProducts(self, settings, products, attribute):
        raise NotImplementedError()

    def export_inventory(self, inventory):
        raise NotImplementedError()

    def updateImages(self, settings, products, attribute):
        raise NotImplementedError()

    def getAttributeTypes(self):
        raise NotImplementedError()

    def getAttributes(self):
        raise NotImplementedError()

    def getExternalLinkForOrder(self):
        raise NotImplementedError()
