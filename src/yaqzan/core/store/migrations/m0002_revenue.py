"""Migration 0002: revenue mirror tables and per-source sync state (F03, METRICS §5).

The revenue tables mirror what a payment vendor says now, so the app may
insert and update rows (a subscription changes status) but never delete them.
They hold vendor ids and amounts only: no names, emails or addresses.

Never edit this file once merged: fix forward with a new migration.
"""

NAME = "revenue"

SQL = r"""
CREATE TABLE yaqzan.revenue_subscriptions (
    tenant_id         uuid NOT NULL,
    project_id        uuid NOT NULL,
    source            text NOT NULL CHECK (source ~ '^[a-z][a-z0-9_]{0,63}$'),
    provider_id       text NOT NULL CHECK (provider_id ~ '^[A-Za-z0-9_\-]{1,128}$'),
    customer_ref      text NOT NULL CHECK (customer_ref ~ '^[A-Za-z0-9_\-]{1,128}$'),
    status            text NOT NULL
        CHECK (status IN ('trialing', 'active', 'past_due', 'paused', 'canceled', 'incomplete')),
    cycle_interval    text NOT NULL CHECK (cycle_interval IN ('day', 'week', 'month', 'year')),
    cycle_frequency   integer NOT NULL CHECK (cycle_frequency BETWEEN 1 AND 366),
    currency          text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    recurring_minor   bigint CHECK (recurring_minor >= 0),
    started_at        timestamptz,
    canceled_at       timestamptz,
    trial_ends_at     timestamptz,
    vendor_updated_at timestamptz NOT NULL,
    synced_at         timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, project_id, source, provider_id)
);

CREATE TABLE yaqzan.revenue_payments (
    tenant_id        uuid NOT NULL,
    project_id       uuid NOT NULL,
    source           text NOT NULL CHECK (source ~ '^[a-z][a-z0-9_]{0,63}$'),
    provider_id      text NOT NULL CHECK (provider_id ~ '^[A-Za-z0-9_\-]{1,128}$'),
    transaction_ref  text NOT NULL CHECK (transaction_ref ~ '^[A-Za-z0-9_\-]{1,128}$'),
    subscription_ref text CHECK (subscription_ref ~ '^[A-Za-z0-9_\-]{1,128}$'),
    customer_ref     text CHECK (customer_ref ~ '^[A-Za-z0-9_\-]{1,128}$'),
    status           text NOT NULL CHECK (status IN ('succeeded', 'failed', 'canceled', 'pending')),
    currency         text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    amount_minor     bigint NOT NULL CHECK (amount_minor >= 0),
    tax_minor        bigint NOT NULL CHECK (tax_minor >= 0 AND tax_minor <= amount_minor),
    error_code       text CHECK (error_code ~ '^[a-z0-9_]{1,64}$'),
    occurred_at      timestamptz NOT NULL,
    synced_at        timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, project_id, source, provider_id)
);
CREATE INDEX revenue_payments_by_time ON yaqzan.revenue_payments (tenant_id, project_id, source, occurred_at);

CREATE TABLE yaqzan.revenue_refunds (
    tenant_id         uuid NOT NULL,
    project_id        uuid NOT NULL,
    source            text NOT NULL CHECK (source ~ '^[a-z][a-z0-9_]{0,63}$'),
    provider_id       text NOT NULL CHECK (provider_id ~ '^[A-Za-z0-9_\-]{1,128}$'),
    kind              text NOT NULL CHECK (kind IN ('refund', 'chargeback', 'chargeback_reversal')),
    status            text NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'reversed')),
    transaction_ref   text NOT NULL CHECK (transaction_ref ~ '^[A-Za-z0-9_\-]{1,128}$'),
    subscription_ref  text CHECK (subscription_ref ~ '^[A-Za-z0-9_\-]{1,128}$'),
    currency          text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    amount_minor      bigint NOT NULL CHECK (amount_minor >= 0),
    tax_minor         bigint NOT NULL CHECK (tax_minor >= 0 AND tax_minor <= amount_minor),
    created_at        timestamptz NOT NULL,
    vendor_updated_at timestamptz NOT NULL,
    synced_at         timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, project_id, source, provider_id)
);
CREATE INDEX revenue_refunds_by_time ON yaqzan.revenue_refunds (tenant_id, project_id, source, created_at);

-- One row per collector per project: when it last tried, last succeeded, and why it last failed.
CREATE TABLE yaqzan.source_syncs (
    tenant_id       uuid NOT NULL,
    project_id      uuid NOT NULL,
    source          text NOT NULL CHECK (source ~ '^[a-z][a-z0-9_]{0,63}$'),
    last_attempt_at timestamptz NOT NULL,
    last_success_at timestamptz,
    last_error_code text CHECK (last_error_code ~ '^[a-z][a-z0-9_.]{0,63}$'),
    cursor_at       timestamptz,
    PRIMARY KEY (tenant_id, project_id, source)
);

REVOKE ALL ON yaqzan.revenue_subscriptions, yaqzan.revenue_payments, yaqzan.revenue_refunds,
    yaqzan.source_syncs FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON yaqzan.revenue_subscriptions, yaqzan.revenue_payments,
    yaqzan.revenue_refunds, yaqzan.source_syncs TO yaqzan_app;
"""
