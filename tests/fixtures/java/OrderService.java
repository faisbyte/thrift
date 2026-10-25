package com.shop;

import com.shop.model.Order;

public class OrderService {
    private final Repo repo;

    /** Create the service. */
    public OrderService(Repo repo) {
        this.repo = repo;
    }

    @Override
    public Order findOrder(long id) {
        return repo.find(id);
    }

    static class Cache {
        void evict() {}
    }
}
