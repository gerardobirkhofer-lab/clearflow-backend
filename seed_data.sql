--
-- PostgreSQL database dump
--

\restrict OYbsJtBrcEPi2G68VLEuIJD2sgUUTK7RkoS3zrmyE1ISi1gDbPVwfi7MOuJVLoH

-- Dumped from database version 15.18
-- Dumped by pg_dump version 15.18

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Data for Name: alembic_version; Type: TABLE DATA; Schema: public; Owner: clearflow
--

INSERT INTO public.alembic_version VALUES ('0e3c5cecf181');


--
-- Data for Name: tenants; Type: TABLE DATA; Schema: public; Owner: clearflow
--

INSERT INTO public.tenants VALUES ('22222222-2222-2222-2222-222222222222', 'Default Tenant', 'default', 'UTC', 'USD', 'YYYY-MM-DD', '.', ',', true, 'free', NULL);


--
-- Data for Name: api_keys; Type: TABLE DATA; Schema: public; Owner: clearflow
--



--
-- Data for Name: audit_logs; Type: TABLE DATA; Schema: public; Owner: clearflow
--



--
-- Data for Name: file_uploads; Type: TABLE DATA; Schema: public; Owner: clearflow
--



--
-- Data for Name: institutions; Type: TABLE DATA; Schema: public; Owner: clearflow
--

INSERT INTO public.institutions VALUES ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'Banco Santander', 'SANTANDER', 'BANK', 2, NULL, NULL, true, NULL, NULL, NULL, NULL, NULL, '22222222-2222-2222-2222-222222222222', '2026-08-03 16:24:38.3326+00', '2026-08-03 16:24:38.3326+00');
INSERT INTO public.institutions VALUES ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'BBVA', 'BBVA', 'BANK', 1, NULL, NULL, true, NULL, NULL, NULL, NULL, NULL, '22222222-2222-2222-2222-222222222222', '2026-08-03 16:24:38.3326+00', '2026-08-03 16:24:38.3326+00');


--
-- Data for Name: bank_movements; Type: TABLE DATA; Schema: public; Owner: clearflow
--

INSERT INTO public.bank_movements VALUES ('ffffffff-ffff-ffff-ffff-ffffffffffff', '2026-08-02', '2026-08-02', '2026-08-03 16:24:38.3326+00', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', NULL, 'CREDIT', 'BATCH-001-SANT', 'Card settlement batch 001', 1000.00, NULL, 'EUR', 'cccccccc-cccc-cccc-cccc-cccccccccccc', true, NULL, NULL, '22222222-2222-2222-2222-222222222222', '2026-08-03 16:24:38.3326+00', '2026-08-03 16:24:38.3326+00');
INSERT INTO public.bank_movements VALUES ('11111111-1111-1111-1111-111111111111', '2026-08-02', '2026-08-02', '2026-08-03 16:24:38.3326+00', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', NULL, 'CREDIT', 'BATCH-002-SANT', 'Card settlement batch 002', 480.00, NULL, 'EUR', 'dddddddd-dddd-dddd-dddd-dddddddddddd', true, NULL, NULL, '22222222-2222-2222-2222-222222222222', '2026-08-03 16:24:38.3326+00', '2026-08-03 16:24:38.3326+00');


--
-- Data for Name: card_collections; Type: TABLE DATA; Schema: public; Owner: clearflow
--

INSERT INTO public.card_collections VALUES ('cccccccc-cccc-cccc-cccc-cccccccccccc', '2026-08-02', '2026-08-03 16:24:38.3326+00', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'DEBIT', NULL, NULL, 'BATCH-001-SANT', 1000.00, NULL, NULL, 12, NULL, 'MATCHED', NULL, NULL, NULL, NULL, NULL, NULL, '22222222-2222-2222-2222-222222222222', '2026-08-03 16:24:38.3326+00', '2026-08-03 16:24:38.3326+00');
INSERT INTO public.card_collections VALUES ('dddddddd-dddd-dddd-dddd-dddddddddddd', '2026-08-02', '2026-08-03 16:24:38.3326+00', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'CREDIT', NULL, NULL, 'BATCH-002-SANT', 500.00, NULL, NULL, 8, NULL, 'DISCREPANCY', NULL, NULL, NULL, NULL, NULL, NULL, '22222222-2222-2222-2222-222222222222', '2026-08-03 16:24:38.3326+00', '2026-08-03 16:24:38.3326+00');
INSERT INTO public.card_collections VALUES ('eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee', '2026-08-02', '2026-08-03 16:24:38.3326+00', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'DEBIT', NULL, NULL, 'BATCH-001-BBVA', 250.00, NULL, NULL, 5, NULL, 'UNMATCHED', NULL, NULL, NULL, NULL, NULL, NULL, '22222222-2222-2222-2222-222222222222', '2026-08-03 16:24:38.3326+00', '2026-08-03 16:24:38.3326+00');


--
-- Data for Name: cash_flow_entries; Type: TABLE DATA; Schema: public; Owner: clearflow
--



--
-- Data for Name: fee_structures; Type: TABLE DATA; Schema: public; Owner: clearflow
--



--
-- Data for Name: morning_reports; Type: TABLE DATA; Schema: public; Owner: clearflow
--

INSERT INTO public.morning_reports VALUES ('bea8f9cb-adcf-4bb2-a81c-6375316f6040', '2026-08-02', '2026-08-03 19:29:24.804838+00', 3, 1, 0, 1, 1, 1480.00, 1730.00, 1730.00, NULL, 0.00, '[{"actual_fee": "15.00", "difference": "10.00", "expected_fee": "5.00", "institution_name": "Banco Santander", "collection_reference": "BATCH-002-SANT"}]', '[{"message": "1 collections remain unmatched and require attention.", "category": "reconciliation", "severity": "WARNING"}, {"message": "1 discrepancies detected in yesterday''s collections.", "category": "discrepancy", "severity": "CRITICAL"}, {"message": "Fee discrepancies totaling €10.00 detected.", "category": "fees", "severity": "WARNING"}]', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, '22222222-2222-2222-2222-222222222222', '2026-08-03 19:29:24.804838+00', '2026-08-03 19:29:24.804838+00');


--
-- Data for Name: tpv_closing_reports; Type: TABLE DATA; Schema: public; Owner: clearflow
--



--
-- Data for Name: reconciliation_results; Type: TABLE DATA; Schema: public; Owner: clearflow
--

INSERT INTO public.reconciliation_results VALUES ('22222222-2222-2222-2222-222222222223', 'cccccccc-cccc-cccc-cccc-cccccccccccc', '2026-08-02', 'ffffffff-ffff-ffff-ffff-ffffffffffff', NULL, 'MATCHED', NULL, 1000.00, 1000.00, NULL, 10.00, 10.00, 0.00, 0.00, NULL, NULL, true, NULL, NULL, '2026-08-03 16:24:38.3326+00', '22222222-2222-2222-2222-222222222222', '2026-08-03 16:24:38.3326+00', '2026-08-03 16:24:38.3326+00');
INSERT INTO public.reconciliation_results VALUES ('22222222-2222-2222-2222-222222222224', 'dddddddd-dddd-dddd-dddd-dddddddddddd', '2026-08-02', '11111111-1111-1111-1111-111111111111', NULL, 'DISCREPANCY', NULL, 500.00, 480.00, NULL, 5.00, 15.00, 10.00, 20.00, NULL, NULL, false, NULL, NULL, '2026-08-03 16:24:38.3326+00', '22222222-2222-2222-2222-222222222222', '2026-08-03 16:24:38.3326+00', '2026-08-03 16:24:38.3326+00');
INSERT INTO public.reconciliation_results VALUES ('22222222-2222-2222-2222-222222222225', 'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee', '2026-08-02', NULL, NULL, 'UNMATCHED', NULL, 250.00, NULL, NULL, 2.50, NULL, NULL, NULL, NULL, NULL, false, NULL, NULL, '2026-08-03 16:24:38.3326+00', '22222222-2222-2222-2222-222222222222', '2026-08-03 16:24:38.3326+00', '2026-08-03 16:24:38.3326+00');


--
-- Data for Name: users; Type: TABLE DATA; Schema: public; Owner: clearflow
--



--
-- Data for Name: webhooks; Type: TABLE DATA; Schema: public; Owner: clearflow
--



--
-- PostgreSQL database dump complete
--

\unrestrict OYbsJtBrcEPi2G68VLEuIJD2sgUUTK7RkoS3zrmyE1ISi1gDbPVwfi7MOuJVLoH

