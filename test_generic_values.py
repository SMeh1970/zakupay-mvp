from generic_identity import alternative


def test_name_with_other_size_offers_that_size_as_the_value():
    assert alternative("Монолитный поликарбонат прозрачный 4 мм 2050х3050", "8мм") == "4мм"
    assert alternative("Монолитный поликарбонат прозрачный 4 мм 1250х2500", "2050x3050") == "1250x2500"


def test_no_other_value_of_the_same_kind_means_no_alternative():
    assert alternative("Монолитный поликарбонат прозрачный", "8мм") is None
    assert alternative("Труба 8 л", "8мм") is None            # other unit is not a value of the same size
    assert alternative("Лазер 6 кВт", "211lcg0035") is None    # plain codes have no comparable shape
    assert alternative("Поликарбонат 8 мм", "8мм") is None     # the same value is not an alternative
