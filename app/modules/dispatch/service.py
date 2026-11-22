from app.modules.dispatch.plan import plan_dispatch


def generate_dispatch(store, run_id):
    existing = store.get_dispatch(run_id, required=False)
    if existing is not None:
        return existing, True
    plan = plan_dispatch(store.get(run_id))
    return store.insert_dispatch(run_id, plan)


def close_dispatch_order(store, run_id, order_id, closer):
    return store.close_dispatch(run_id, order_id, closer)
