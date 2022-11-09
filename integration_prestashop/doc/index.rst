==========================
 Quick configuration guide
==========================

|

1. Install the Odoo PrestaShop Connector on your Odoo server – How to install in `Odoo.sh (video) <https://youtu.be/-mToY8rOcCA>`__

    Note that on odoo.sh you will need to add requirements.txt file to the root of your repository with the following content. So additional Python libraries will be installed. On your own instance you will just need to install them manually via pip install command

    | ``git+https://github.com/prestapyt/prestapyt.git@f9898b4245611b487e4592f601a22a07de0a26cc``
    | ``Cerberus==1.3.2``

|

2. Our connector is using queue_job from OCA.  If you are on odoo.sh, skip this section and jump to the next one. Below is quick summary of what you need to add at the end of your odoo.conf file. If you are interested in full documentation, check it out `here <https://apps.odoo.com/apps/modules/16.0/queue_job/>`__.

| ``workers = 2 ; set here amount of workers higher than 1``
| ``server_wide_modules = web,queue_job ; add queue_job to server wide modules``
| ``[queue_job]``
| ``channels = root:1``

|

3. Special note for deploying to odoo.sh (`also shown on the video <https://youtu.be/-mToY8rOcCA>`__):

-  Config file can be found when entering shell in the following location "/home/odoo/.config/odoo/odoo.conf". Add there the following configs:

| ``server_wide_modules = web,queue_job``
| ``[queue_job]``
| ``channels = root:1``
| ``scheme = https``
| ``host = <your_odoo_host> (e.g. myhost.odoo.com)``
| ``port = 443``
|
- After changing the configuration file, run ``odoosh-restart`` command in the shell

|

4. Then follow steps from this video to make an initial configuration of our Odoo PrestaShop connector (`video <https://youtu.be/4sop_8WYMWw>`__)

|

Feedback
##########

- In case of any issues, please contact us at support@ventor.tech
- Don't forget to share your experience after you go live :)

  | (only person who made a purchase, can leave ratings)

|

Change Log
##########

|

* 1.8.3 (2022-11-07)
    - Added compatibility with partner_firstname module from OCA
    - Fixed import of gift line

* 1.8.2 (2022-10-28)
    - Fixed Feature Value creation
    - Fixed “Import External Records“ running for Product Variants from Jobs
    - Fixed calculation of discount in Odoo if there are several taxes in sales order

* 1.8.1 (2022-10-18)
    - Import customers functionality was not working with all queue_job module versions

* 1.8.0 (2022-10-10)
    - NEW! Allow exporting of product quantities both in real-time and by cron. Make it configurable on the “Inventory“ tab on sales integration.
    - NEW! Allow defining which field should be synchronized when sending the stock to the e-Commerce system. Allowing 3 options: “Free To Use Quantity“, “On Hand Quantity” and  “Forecasted Quantity”.
    - NEW! Implemented wizard allowing to import customers based on the last update date.
    - NEW! Implementing Gift Wrap synchronization from Prestashop to Odoo as a separate line in sales orders.
    - NEW! Added setting to allow automatic creation of Delivery Carrier and Taxes in Odoo if the existing mapping is not found (during initial import and during Sales Order Import).
    - Fix issue with auto-workflow failing in some cases when SO status is changing on webhook.
    - When an order is created with an existing partner make sure to also emulate the selection of partner on the Odoo interface so needed fields from the partner will be filled in (Payment Terms, Fiscal Positions and etc.) 
    - TECHNICAL! Improve the retry mechanism for importing products and executing workflow actions to workaround concurrent update errors in some cases (e.g. sales order was not auto-confirmed and remained in draft state)
    - Do not create webhooks automatically in case integration is activated. Users need to do it manually by clicking the “Create Webhooks“ button on “Webhooks“ tab inside integration.
    - Set the proper fiscal position on automatic order import according to Fiscal Position settings.
    - Improved manual mapping of product variants and product templates in case template has only 1 variant.

* 1.7.1 (2022-09-08)
    - Added possibility to specify additional field where Sales Order reference from Prestashop will be added (for example "Client Reference" field on SO) `(watch video) <https://youtu.be/Fmx80pKh4Vc>`__.
    - Fix synchronization of newsletter subscription status
    - Sales Order date is now set equal to Order creation date from the Prestashop
    - Improve functionality for partners creation (first search partner by full address, before creating a new one)

* 1.7.0 (2022-09-05)
    - **NEW!** Major feature. Introduced auto workflow that allows based on sales order status: to validate sales order, create and validate invoice for it and register payment on created invoice. Configuration is flexible and can be done individually for every SO status `(watch video) <https://youtu.be/DEskoCQ-4Ek>`__.
    - **NEW!** Added automatic creation of Webhooks to track Order Status change on the Prestashop side. Requires paid third-party module from Prestashop addons webshop “Webhooks integration Module“ Link to module https://addons.prestashop.com/en/third-party-data-integrations-crm-erp/48921-webhooks-integration.html `(watch video) <https://youtu.be/cqXjQ6_4I24>`__.
    - **NEW!** Auto-cancel Sales Order on Odoo side when Order is Cancelled on Prestashop side. Requires paid third-party module from Prestashop addons webshop “Webhooks integration Module“ (see link above) `(watch video) <https://youtu.be/uIJc7pzoFzs>`__.
    - **NEW!** Change Sales Order sub-status to "Shipped" when all transfers related to it are "Done" or "Cancelled" `(watch video) <https://youtu.be/-j5pdsHS9z4>`__.
    - **NEW!** Save to Odoo newsletter subscription status for the customer (is subscribed?,  date of subscription, date of user Registration). Only set during first customer creation. `(watch video) <https://youtu.be/WfdN3FhFYaE>`__.
    - **NEW!** Separate functionality of products mapping (trying to map with existing Odoo Product) from products import (trying to map and if not found create product in Odoo) `(watch video) <https://youtu.be/hNqCVyv5fcY>`__.
    - Allow to disable export of product images from Odoo to Prestashop (checkbox on Integration form -> "Product Defaults" tab)
    - When carrier details are changed on Prestashop side, no need to add mapping of delivery carrier again in Odoo.
    - During creation of sales order if mapping for product was not found trying to auto-map by reference OR barcode with existing Odoo Product before failing creation of sales order.
    - Send tracking numbers only when sales order is fully shipped (all related pickings are either "done" or "cancelled" and there are at least some delivered items).
    - Import from Prestashop to Odoo only Feature Values that are connected to some Feature.
    - Fix issue with synchronizing records (features, attributes and etc) with special symbols in their name ("%", "_" , etc.)
    - Fix issue with impossibility to import orders with deleted customer (set "Default Customer" on Sale Integration -> "Sale Order Details" tab)
    - TECHNICAL: Added possibility to easier extend product search criteria (for importing and validating products).
    - TECHNICAL: Updated prestapyt library to new version 0.11.1 to remove deprecated warnings for Python 3 (See requirements.txt file in the module)
    - TECHNICAL Improved Performance to allow importing of 150 000+ products from Prestashop.

* 1.6.0 (2022-07-21)
    - **NEW!** Automatically Cancel order on Prestashop when it is marked as Cancelled on Odoo side.
    - **NEW!** Product Features: Synchronize from Prestashop to Odoo during initial import `(watch video) <https://www.youtube.com/watch?v=6ucwcLhhOlw>`__.
    - **NEW!** Product Features: Sync from Odoo to Prestashop (when changing/creating on Odoo side) `(watch video) <https://www.youtube.com/watch?v=6ucwcLhhOlw>`__.
    - **NEW!** Synchronise Optional Products from Odoo to Prestashop (requires to add Optional Products field to fields mapping) `(watch video) <https://www.youtube.com/watch?v=6ucwcLhhOlw>`__.
    - **NEW!** Add possibility to synchronize optional products from Odoo to Prestashop `(watch video) <https://www.youtube.com/watch?v=6ucwcLhhOlw>`__.
    - Search only for active combinations when validating Prestashop products for duplicates.
    - When creating sales order from Prestashop, also set current sales order status as it is in Presta.
    - Fix issue with product validation results when Prestashop admin URL cannot be opened (if contains uppercase letters).
    - Add compatibility for older Prestashop versions where on order row there is no id_customization.
    - Added the ability to work both with the Manufacturing module and without it.
    - Added the ability to work both with the eCommerce module and without it.
    - Add possibility to Synchronize Products Cost Price from Odoo to Prestashop.
    - Improve categories synchronisation (automatically sync parent categories together with child, remove Root category from initial synchronisation as it is useless) `(watch video) <https://www.youtube.com/watch?v=XNNHPlNPoLk>`__.
    - TECHNICAL: Added possibility to easily extend module for adding custom fields `(watch video) <https://www.youtube.com/watch?v=sBXCKvOdQ9w>`__.
    - Validate Countries and States for duplicates and if any found, then show error message with list of all problematic countries/states.

* 1.5.5 (2022-06-16)
    - Do not delete redundant combinations on Prestashop side in case we unset checkbox for specific integration on the Product
    - Fix issue with initial creation of Product with variants when checkbox for integration is set
    - Automatically cleanup non-existing external product and product variants records (in case not found in Prestashop)
    - Before exporting products from Odoo to Prestashop double check that same product already exists in Presta. If exists then map it automatically by internal reference
    - Fix issue with not downloading of products with customizations

* 1.5.4 (2022-06-12)
    - Download tax rules at the same time as downloading taxes
    - Associate automatically tax rules with taxes

* 1.5.3 (2022-06-02)
    - Allow definition of the mapping between taxes and tax rules using Quick Configuration Wizard
    - Improve product taxes import and export between Odoo and Prestashop (using taxes/tax rules mapping)
    - Fix shipping taxes calculations (now possible to have more then one tax on shipping line)
    - Added functionality to import payment transactions (containing transaction_id) to Odoo. It is using OCA module sale_advance_payment

* 1.5.2 (2022-05-16)
    - Solve issue with multi-company setup and automatic sales order download
    - Synchronize all countries from Prestashop (not only active)
    - Set proper currency on Sales Order if it is different from company standard
    - Multi-step delivery: Send tracking number ONLY for outgoing picking

* 1.5.1 (2022-05-09)
    - Retrieve only active states from Prestashop

* 1.5.0 (2022-05-01)
    - Added Quick Configuration Wizard
    - Added taxes and tax groups quick manual import
    - Version of prestapyt library changed to 0.10.1
    - Fixed initial payment methods import
    - Fixed import BOMs with no product variant components
    - Fixed incorrect tax rate applied to order shipping line
    - When integration is deleted, also delete related Sales Order download Scheduled Action
    - When importing sales order, payment method is also created if it doesn't exist

* 1.4.4 (2022-04-20)
    - Added filter by active countries and states in initial import
    - Fixed order import when line has several taxes
    - Fixed product import

* 1.4.3 (2022-03-31)
    - Added import of payment method before creating an order if it does not exists
    - Added integration info in Queue Job for errors with mapping
    - Added possibility to import product categories by action “Import Categories“ in menus “External → Categories“ and “Mappings → Categories“
    - Added button "Import Product" on unmapped products in menu “Mapping → Products“
    - Fixed issue with export new products
    - Fixed product and product variant mapping in initial import
    - Fixed empty external names after export products and import orders

* 1.4.2 (2022-03-11)
    - Sale order line description for discount and price difference is assigned from product

* 1.4.1 (2022-03-01)
    - Fix issue with difference per cent of the total order amount

* 1.4.0 (2022-02-17)
    - Added possibility to import product attributes and values by action “Import Products Attributes“ in menus “External → Product Attributes“ and “Mappings → Product Attributes“
    - Added creation of Order Discount from e-Commerce System as a separate product line in a sell order
    - Fix issue with trying to send stock to Prestashop for products that has disabled integration
    - Fix bug of mapping modification for users without role Job Queue Manager

* 1.3.8 (2022-01-05)
    - Added export of "Delivery time of in-stock products" and "Delivery time of out-of-stock products with allowed orders" fields

* 1.3.7 (2021-12-31)
    - Added button "Import Stock Levels" to “Initial Import“ tab that tries to download stock levels for storable products
    - Fixed bug of delivery line tax calculation
    - Fixed multiple timezone bug in Prestashop

* 1.3.6 (2021-12-24)
    - Added “Initial Import“ tab with two separate buttons into “Sale Integration“:
        - “Import Master Data“ - download and try to map common data
        - “Import products“ - try to import products from e-Commerce System to Odoo (with pre-validation step)
    - Added possibility to import products by action Import Products in menu “External → Products“
    - Import of products is run in jobs separately for each product

* 1.3.5 (2021-11-22)
    - Downloaded sales order now is moved from file to JSON format and can be edited/viewed in menu “e-Commerce Integration → Sales Raw Data“

* 1.3.4 (2021-10-27)
    - Synchronize tracking only after it is added to the stock picking. Some carrier connectors

* 1.3.3 (2021-10-21)
    - Fix issue with Combinations not exporting properly attribute values

* 1.3.2 (2021-10-19)
    - Fix issues with incorrect categories syncing

* 1.3.1 (2021-10-18)
    - Added synchronization of partner language and partner email (to delivery and shipping address)

* 1.3 (2021-10-02)
    - Automapping of the Countries, Country States, Languages, Payment Methods
    - Added Default Sales Team to Sales Order created via e-Commerce Integration
    - Added synchronization of VAT and Personal Identification Number field
    - In case purchase is done form the company, create Company and Contact inside Odoo

* 1.2.1 (2021-09-21)
    - Fixed regression issue with initial creation of the product with combination not working properly

* 1.2 (2021-09-20)
    - Added possibility to define field mappings and specify if field should be updatable or not
    - Avoid creation of duplicated products under some conditions

* 1.1 (2021-06-28)
    - Add field for Delivery Notes on Sales Order
    - Added configuration to define on Sales Integration which fields should be used on SO and Delivery Order for Delivery Notes
    - Allow to specify which product should be exported to which channel
    - Add separate field that allows to specify Product Name to be sent to e-Commerce site instead of standard name
    - Do not change Minimal Order Quantity on existing Combinations

* 1.0.4 (2021-06-01)
    - Fix variants import if no variants exists

* 1.0.3 (2021-05-28)
    - Replaced client request to new format (fixing payment and delivery methods retrieving)
    - Fixed warnings on Odoo.sh with empty description on new models

* 1.0.2 (2021-04-21)
    - Fixed errors during import external models
    - Fixed images export

* 1.0.1 (2021-04-13)
    - Added PS_TIMEZONE settings field to correctly handle case when PrestaShop is in different timezone
    - Added Check Connection support

* 1.0 (2021-03-23)
    - Odoo integration with PrestaShop

|
