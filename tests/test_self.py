from agent.self_description import describe


def test_how_i_work_comes_from_the_model_without_the_build_list():
    text = describe()
    for role in ("Assistant", "Director", "Researcher", "Manager"):
        assert role in text
    assert "Postgången" in text and "Schemat" in text
    assert "Från i dag till modellen" not in text and "Status:" not in text
