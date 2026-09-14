import unittest

from zakupay_email import parse_zakupay_email


def sample_email(order_id: int, customer: str) -> bytes:
    return f"""From: zakupay@sel-be.ru
To: 1043324@gmail.com
Subject: =?utf-8?b?0JDQstGC0L7Qt9Cw0L/RgNC+0YE=?= [zakupay id {order_id}]
MIME-Version: 1.0
Content-Type: multipart/alternative; boundary=event

--event
Content-Type: text/plain; charset=utf-8

Text version is unavailable
--event
Content-Type: text/html; charset=utf-8

<html><body><h1>Запрос предложения</h1>
<p>Прошу выставить счет на заявку ID {order_id}</p>
<p>Заказчик: {customer}</p>
<a href="https://prodavay.sel-be.ru/core/supplier/registry?requestId=1&amp;id={order_id}&amp;action=send">Отправить предложение</a>
</body></html>
--event--
""".encode("utf-8")


class ZakupayEmailParserTests(unittest.TestCase):
    def test_autorequest_examples(self):
        cases = (
            (12345601, 'ООО "ЗАКАЗЧИК А"'),
            (12345602, 'ООО "ЗАКАЗЧИК А"'),
            (12345603, 'ООО "ЗАКАЗЧИК Б"'),
        )
        for order_id, customer in cases:
            with self.subTest(order_id=order_id):
                event = parse_zakupay_email(sample_email(order_id, customer))
                self.assertEqual(event.order_id, order_id)
                self.assertEqual(event.event_type, "new_order")
                self.assertEqual(event.sender, "zakupay@sel-be.ru")

    def test_rejects_email_without_order_id(self):
        with self.assertRaisesRegex(ValueError, "order ID"):
            parse_zakupay_email(b"From: zakupay@sel-be.ru\nSubject: notice\n\nNo order")


if __name__ == "__main__":
    unittest.main()
