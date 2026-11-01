CREATE TABLE hn_item (id INTEGER PRIMARY KEY, by TEXT, time TEXT, text TEXT, parent INTEGER, story_id INTEGER,
                      story_title TEXT, parent_text TEXT);
CREATE TABLE category (id INTEGER PRIMARY KEY, slug TEXT UNIQUE, name TEXT, parent_id INTEGER);
CREATE TABLE product (id INTEGER PRIMARY KEY, slug TEXT UNIQUE, name TEXT, domain TEXT, github_repo TEXT,
                      category_id INTEGER REFERENCES category(id), pricing TEXT, oss INTEGER, description TEXT,
                      official_url TEXT, logo_url TEXT);
CREATE TABLE alias (product_id INTEGER REFERENCES product(id), raw_name TEXT, n INTEGER);
CREATE TABLE mention (id INTEGER PRIMARY KEY, item_id INTEGER REFERENCES hn_item(id), product_id INTEGER REFERENCES product(id),
                      raw_name TEXT, stance TEXT, reason_quote TEXT, quote_verbatim INTEGER, use_case TEXT,
                      alternatives TEXT, switched_from TEXT, recommender_context TEXT, affiliated INTEGER,
                      category_raw TEXT, extractor_version TEXT, confidence REAL);
CREATE INDEX mention_product ON mention(product_id);
CREATE INDEX mention_item ON mention(item_id);
CREATE TABLE theme (id INTEGER PRIMARY KEY, product_id INTEGER, polarity TEXT, label TEXT, summary TEXT,
                    mention_ids TEXT, synth_version TEXT);
CREATE TABLE verdict (product_id INTEGER PRIMARY KEY, text TEXT, clauses TEXT, cited_mention_ids TEXT,
                      use_case_tags TEXT, synth_version TEXT, generated_at TEXT);
CREATE TABLE product_score (product_id INTEGER PRIMARY KEY, category_id INTEGER, distinct_recommenders INTEGER,
                            distinct_commenters INTEGER, warners INTEGER, weighted_score REAL, rising_score REAL,
                            first_mention TEXT, latest_mention TEXT, sentiment_by_month TEXT, sentiment_by_year TEXT);
CREATE TABLE comparison (product_a INTEGER, product_b INTEGER, co_mentions INTEGER, switched_a_to_b INTEGER,
                         switched_b_to_a INTEGER, PRIMARY KEY (product_a, product_b));
CREATE TABLE alias_review (id INTEGER PRIMARY KEY, raw_name TEXT, candidate_product_ids TEXT, status TEXT,
                           decided_by TEXT, note TEXT);
CREATE TABLE correction (id INTEGER PRIMARY KEY, mention_id INTEGER, kind TEXT, note TEXT, status TEXT);
CREATE VIRTUAL TABLE mention_fts USING fts5(product, quote, context);
