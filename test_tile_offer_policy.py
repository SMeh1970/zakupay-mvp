import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from tile_offer_policy import competitor_prices, due_at, evaluate, urgent
from tile_autopilot import fetch_prices
from test_porcelain import PRODUCT, CUSTOMER


CLOCK = datetime(2026, 10, 22, 8, tzinfo=timezone.utc)


class AutomaticPolicyTests(unittest.TestCase):
    def setUp(self):
        self.product = {**PRODUCT, 'stock_m2':500, 'price_rub_m2':1000,
                        'stock_as_of':(CLOCK-timedelta(hours=12)).isoformat()}
        self.catalog = {'id':42, 'data':{'products':[self.product]}}
        self.item = {'id':101,'goodName':PRODUCT['name']+' керамогранит серый под мрамор 600х600х9 мм матовый',
                     'count':100,'unit':{'name':'м²'},'analogAllow':False}
        self.order = {'id':123,'creationDate':(CLOCK-timedelta(hours=24)).isoformat(),
                      'finishDate':'2026-11-01','name':'Керамогранит','customer':CUSTOMER,
                      'deliveryAddress':'Москва, Тестовая, 1','orderItems':[self.item]}
        self.best = {'101':{'price':1300,'unit':'м²','currency':'RUB','vat_rate':.22,
                            'source':'zakupay:getoffersdeviationpercent','delivery_included':True,
                            'checked_at':CLOCK.isoformat()}}
        self.delivery = {'price':0,'address':self.order['deliveryAddress'],'confirmed':True,
                         'valid_until':(CLOCK+timedelta(hours=1)).isoformat()}

    def run_policy(self, **kwargs):
        return evaluate(self.order,self.catalog,self.best,clock=CLOCK,seller={'inn':'5638073633'},
                        vat_rate=.22,delivery=self.delivery,**kwargs)

    def test_full_day_boundary_and_offsets(self):
        self.order['creationDate']='2026-10-21T11:00:01+0300'
        self.assertEqual(self.run_policy()['status'],'waiting')
        self.order['creationDate']='2026-10-21T11:00:00+0300'
        self.assertEqual(self.run_policy()['status'],'ready')
        self.order['creationDate']='2026-10-21T11:00:00'
        self.assertIsNone(due_at(self.order))
        self.assertEqual(self.run_policy()['status'],'blocked')

    def test_urgent_only_removes_delay_not_match_or_floor_guards(self):
        self.order.update(creationDate=CLOCK.isoformat(),name='Срочно! Керамогранит')
        self.assertTrue(urgent(self.order));self.assertEqual(self.run_policy()['status'],'ready')
        self.best['101']['price']=900
        self.assertEqual(self.run_policy()['status'],'blocked')
        self.order['name']='Не срочно'
        self.assertFalse(urgent(self.order))

    def test_five_percent_and_both_floor_formats(self):
        r=self.run_policy();self.assertEqual(r['lines'][0]['proposed_unit_price'],1235)
        self.best['101']['price']=960
        self.assertTrue(any('минимальн' in x for x in self.run_policy()['blockers']))
        self.item['goodName']=self.item['goodName'].replace('600х600','600х1200')
        self.product['format']='60x120'
        self.best['101']['price']=1500
        self.assertEqual(self.run_policy()['lines'][0]['floor'],1440)
        self.assertEqual(self.run_policy()['status'],'blocked')
        self.best['101']['price']=1600
        self.assertEqual(self.run_policy()['status'],'ready')

    def test_kopecks_never_round_up_past_five_percent(self):
        self.best['101']['price']=1000.01
        self.assertEqual(self.run_policy()['lines'][0]['target_price'],950)

    def test_name_is_not_a_numeric_best_price(self):
        self.item['bestOfferItem']='Товар конкурента 999 рублей'
        self.best={}
        self.assertTrue(any('числовой' in x for x in self.run_policy()['blockers']))

    def test_explicit_price_provenance_units_vat_currency_and_delivery_required(self):
        for key,value in [('source','manual'),('unit','шт'),('currency','USD'),
                          ('vat_rate',None),('delivery_included',None),
                          ('checked_at',(CLOCK-timedelta(minutes=11)).isoformat())]:
            with self.subTest(key=key):
                original=self.best['101'][key];self.best['101'][key]=value
                self.assertEqual(self.run_policy()['status'],'blocked')
                self.best['101'][key]=original

    def test_delivered_comparison_keeps_delivery_out_of_product_floor(self):
        self.best['101']['price']=1000
        self.delivery['price']=5000
        self.assertEqual(self.run_policy()['status'],'blocked')
        self.best['101']['delivery_included']=False
        self.assertEqual(self.run_policy()['status'],'ready')

    def test_other_region_needs_real_quote_not_moscow_tariff(self):
        self.order['deliveryAddress']='Троицк, Челябинская область, Лесная, 6'
        self.assertEqual(self.run_policy()['status'],'blocked')
        self.delivery['address']=self.order['deliveryAddress']
        self.delivery['price']=20000
        self.assertEqual(self.run_policy()['status'],'ready')
        self.delivery['valid_until']=(CLOCK-timedelta(seconds=1)).isoformat()
        self.assertEqual(self.run_policy()['status'],'blocked')

    def test_unknown_stock_date_is_not_replaced_by_catalog_import_date(self):
        self.catalog['imported_at']=CLOCK.isoformat()
        for stock_date in [None,(CLOCK-timedelta(hours=49)).isoformat(),(CLOCK+timedelta(hours=1)).isoformat()]:
            self.product['stock_as_of']=stock_date
            self.assertEqual(self.run_policy()['status'],'blocked')

    def test_wrong_color_size_missing_model_and_multiple_colors_need_review(self):
        original=self.item['goodName']
        for name in [original.replace('серый','белый'),original.replace('600х600','600х1200'),
                     original+' CW01', original+' белый', original+' по проекту']:
            with self.subTest(name=name):
                self.item['goodName']=name
                self.assertEqual(self.run_policy()['status'],'blocked')
        self.item['goodName']='Керамогранит серый под мрамор 600х600х9 мм матовый'
        self.assertEqual(self.run_policy()['status'],'blocked')
        self.item['analogAllow']=True
        self.assertEqual(self.run_policy()['status'],'ready')

    def test_glue_quantity_is_not_tile_quantity_and_combined_stock_is_checked(self):
        self.order['orderItems'].append({'id':102,'goodName':'Клей для керамогранита','count':10000,'unit':{'name':'кг'}})
        self.assertEqual(len(self.run_policy()['lines']),1)
        self.item['count']=300
        other=copy.deepcopy(self.item);other['id']=103
        self.order['orderItems'].append(other);self.best['103']=dict(self.best['101'])
        self.assertEqual(self.run_policy()['status'],'blocked')

    def test_stock_zero_unknown_customer_and_expired_request_block(self):
        self.product['stock_m2']=0
        self.assertEqual(self.run_policy()['status'],'blocked')
        self.product['stock_m2']=500;self.order['customer']={}
        self.assertEqual(self.run_policy()['status'],'blocked')
        self.order['customer']=CUSTOMER;self.order['finishDate']='2026-10-21'
        self.assertEqual(self.run_policy()['status'],'blocked')

    def test_price_endpoint_exact_shape_and_duplicate_ids(self):
        self.assertEqual(competitor_prices([{'orderItemId':101,'bestPrice':'Имя'}],CLOCK.isoformat()),{})
        row={'orderItemId':101,'bestPrice':{'price':1000}}
        with self.assertRaises(ValueError):competitor_prices([row,row],CLOCK.isoformat())
        result=competitor_prices([row],CLOCK.isoformat())
        self.assertIsNone(result['101']['delivery_included'])
        self.assertIsNone(result['101']['unit'])

    @patch('tile_autopilot.requests.get')
    @patch('tile_autopilot.requests.post')
    def test_best_price_probe_is_read_only_exact_endpoint_no_redirects(self, post, get):
        post.return_value=Mock(status_code=401)
        get.return_value=Mock(status_code=200)
        prices,error=fetch_prices(self.order,'https://prodavay.sel-be.ru',lambda:{'ZakupayToken':'test'})
        self.assertEqual(prices,{});self.assertIn('401',error)
        self.assertIn('контроль API заявок: HTTP 200',error)
        self.assertEqual(get.call_args_list[0].args[0],'https://prodavay.sel-be.ru/api/v1/orders')
        self.assertEqual(get.call_args_list[1].args[0],'https://prodavay.sel-be.ru/api/v1/orders/123/offer-compare-report')
        self.assertEqual(post.call_args.args[0],'https://prodavay.sel-be.ru/core/supplier/getoffersdeviationpercent')
        self.assertEqual(post.call_args.kwargs['json'],[101])
        self.assertIs(post.call_args.kwargs['allow_redirects'],False)


if __name__=='__main__':unittest.main()
