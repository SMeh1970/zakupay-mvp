import copy
import unittest
from comparison_prototype import matrix_model, save_state
from supplier_comparison import mutate


def sample():
    a=dict(supplier='А',name='Труба 219х6 ст20 ГОСТ 8732',unit='т',qty=14.026,price=138100,sku='a')
    b=dict(supplier='Б',name='Труба 219х6 ст20 ГОСТ 8732',unit='т',qty=14.026,price=131145,sku='b')
    return dict(items=[dict(position=1,requested_name='Труба 219х6 ст20',unit='м',quantity=430,decision='review',candidates=[a,b])],status='needs_review')


class PrototypeTests(unittest.TestCase):
    def test_supplier_quantity_and_price_are_not_normalized(self):
        data=matrix_model(sample())
        a=data['offers']['А'][1]
        self.assertEqual((a['qty'],a['unit'],a['price']),(14.026,'т',138100))
        self.assertAlmostEqual(a['qty']*a['price'],1936990.6)

    def test_search_rejections_are_not_revived(self):
        result=sample();item=result['items'][0]
        item['rejected_candidates']=item.pop('candidates')
        self.assertEqual(matrix_model(result)['offers'],{})

    def test_excluded_review_is_initially_unchecked(self):
        result=sample();result['items'][0]['decision']='excluded'
        self.assertFalse(matrix_model(result)['offers']['А'][1]['enabled'])

    def test_saving_comparison_preserves_review_and_invoice(self):
        result=sample();result['invoice_number']=278
        before=copy.deepcopy(result);matrix=matrix_model(result)
        state=copy.deepcopy(matrix['state']);state['cellOn']={'1_0':True,'1_1':False}
        state['diffState']={'1_0_unit':'accepted','1_1_unit':'accepted','1_0_nameword_3':'rejected'}
        mutate(result,dict(action='prototype_state',signature=matrix['signature'],state=state))
        self.assertEqual(matrix_model(result)['state'],state)
        result.pop('prototype_comparison');self.assertEqual(result,before)

    def test_changed_quotes_do_not_reuse_word_indices(self):
        result=sample();matrix=matrix_model(result)
        save_state(result,dict(signature=matrix['signature'],state=matrix['state']))
        result['items'][0]['candidates'][0]['name']='Другое наименование'
        self.assertNotEqual(matrix_model(result)['signature'],matrix['signature'])
        with self.assertRaises(ValueError):save_state(result,dict(signature=matrix['signature'],state=matrix['state']))

    def test_invalid_state_is_rejected(self):
        result=sample();matrix=matrix_model(result)
        state=copy.deepcopy(matrix['state']);state['diffState']={'1_0_nameword_999':'accepted'}
        with self.assertRaises(ValueError):save_state(result,dict(signature=matrix['signature'],state=state))
