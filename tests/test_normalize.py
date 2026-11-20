from bellhaven_sync.normalize import is_po_box, name_similarity, norm_phone, norm_street, norm_zip, same_name


def test_street_spellings_compare_equal():
    assert norm_street("700 Carpenter Rd") == norm_street("700 Carpenter Road")
    assert norm_street("1120 W Main St") == norm_street("1120 West Main Street")
    assert norm_street("4850 NW Sylvania Ave") == norm_street("4850 Northwest Sylvania Avenue")
    assert norm_street("3313 Wilmington Pike") == norm_street("3313 Wilmington Pk")
    assert norm_street("1125 Logan Blvd") == norm_street("1125 Logan Boulevard")
    assert norm_street("700 Carpenter Rd") != norm_street("701 Carpenter Rd")


def test_zip_phone_po_box():
    assert norm_zip("45662-1234") == "45662"
    assert norm_phone("(330) 431-2495") == norm_phone("330.431.2495") == "3304312495"
    assert is_po_box("PO Box 517") and is_po_box("P.O. Box 9") and not is_po_box("517 Box Elder Rd")


def test_names():
    assert same_name("Bellhaven Rehabilitation & Nursing of Grove City", "Bellhaven Rehab and Nursing of Grove City")
    assert same_name("Bellhaven Healthcare Centre of Ashland", "Bellhaven Health Care Center of Ashland")
    assert same_name("The Arbors at Bellhaven - Dayton", "Arbors at Bellhaven Dayton")
    assert not same_name("Bellhaven of Chagrin Falls", "Riverbend Manor Care Center")
    # Brand words are ignored so two different Bellhaven communities don't look alike.
    assert name_similarity("Bellhaven of Marion", "Bellhaven of Toledo", {"bellhaven"}) < 0.6
