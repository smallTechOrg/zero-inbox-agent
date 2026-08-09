from graph.edges import guard, route_after_history, route_after_llm


class TestGuard:
    def test_passes_through_when_no_error(self):
        assert guard("fetch_items")({"error": None}) == "fetch_items"

    def test_diverts_to_handle_error(self):
        assert guard("fetch_items")({"error": "boom"}) == "handle_error"

    def test_missing_error_key_is_treated_as_healthy(self):
        assert guard("finalize")({}) == "finalize"


class TestRouteAfterHistory:
    def test_llm_when_queue_non_empty(self):
        assert route_after_history({"llm_queue": [{"id": "a"}]}) == "llm"

    def test_skip_llm_when_tiers_1_2_resolved_everything(self):
        assert route_after_history({"llm_queue": []}) == "skip_llm"

    def test_error_short_circuits(self):
        assert route_after_history({"llm_queue": [{}], "error": "x"}) == "handle_error"


class TestRouteAfterLlm:
    def test_deep_when_unsure_items_exist(self):
        assert route_after_llm({"deep_queue": [{"id": "a"}]}) == "deep"

    def test_done_when_nothing_unsure(self):
        assert route_after_llm({"deep_queue": []}) == "done"

    def test_error_short_circuits(self):
        assert route_after_llm({"deep_queue": [], "error": "x"}) == "handle_error"
