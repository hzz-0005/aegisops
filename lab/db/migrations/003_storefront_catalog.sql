ALTER TABLE lab.inventory_items
    ADD COLUMN name varchar(120),
    ADD COLUMN description varchar(240),
    ADD COLUMN category varchar(60);

UPDATE lab.inventory_items
SET name = '链路测试商品',
    description = '用于端到端支付与故障回放的基础商品。',
    category = '实验商品'
WHERE sku = 'sku-demo';

INSERT INTO lab.inventory_items
    (sku, available, unit_price_cents, currency, name, description, category)
VALUES
    ('sku-headphones', 48, 29900, 'CNY', '无线头戴耳机', '主动降噪与长续航，适合通勤。', '数码'),
    ('sku-keyboard', 36, 45900, 'CNY', '机械键盘', '紧凑布局与热插拔轴体。', '数码'),
    ('sku-lamp', 62, 18900, 'CNY', '桌面阅读灯', '柔和照明与多档色温。', '家居')
ON CONFLICT (sku) DO NOTHING;

ALTER TABLE lab.inventory_items
    ALTER COLUMN name SET NOT NULL,
    ALTER COLUMN description SET NOT NULL,
    ALTER COLUMN category SET NOT NULL;
