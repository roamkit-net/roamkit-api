--
-- PostgreSQL database dump
--

-- Dumped from database version 16.4 (Debian 16.4-1.pgdg110+2)
-- Dumped by pg_dump version 16.4 (Debian 16.4-1.pgdg110+2)

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

ALTER TABLE IF EXISTS ONLY public.orders_order DROP CONSTRAINT IF EXISTS orders_order_user_id_e9b59eb1_fk_accounts_user_id;
ALTER TABLE IF EXISTS ONLY public.orders_order DROP CONSTRAINT IF EXISTS orders_order_package_id_9cdaef81_fk_catalog_package_id;
ALTER TABLE IF EXISTS ONLY public.esims_esim DROP CONSTRAINT IF EXISTS esims_esim_user_id_bc77185c_fk_accounts_user_id;
ALTER TABLE IF EXISTS ONLY public.esims_esim DROP CONSTRAINT IF EXISTS esims_esim_order_id_edcc2d53_fk_orders_order_id;
ALTER TABLE IF EXISTS ONLY public.django_admin_log DROP CONSTRAINT IF EXISTS django_admin_log_user_id_c564eba6_fk_accounts_user_id;
ALTER TABLE IF EXISTS ONLY public.django_admin_log DROP CONSTRAINT IF EXISTS django_admin_log_content_type_id_c4bce8eb_fk_django_co;
ALTER TABLE IF EXISTS ONLY public.catalog_package DROP CONSTRAINT IF EXISTS catalog_package_location_id_28aed20c_fk_catalog_location_id;
ALTER TABLE IF EXISTS ONLY public.auth_permission DROP CONSTRAINT IF EXISTS auth_permission_content_type_id_2f476e4b_fk_django_co;
ALTER TABLE IF EXISTS ONLY public.auth_group_permissions DROP CONSTRAINT IF EXISTS auth_group_permissions_group_id_b120cbf9_fk_auth_group_id;
ALTER TABLE IF EXISTS ONLY public.auth_group_permissions DROP CONSTRAINT IF EXISTS auth_group_permissio_permission_id_84c5c92e_fk_auth_perm;
ALTER TABLE IF EXISTS ONLY public.accounts_user_user_permissions DROP CONSTRAINT IF EXISTS accounts_user_user_p_user_id_e4f0a161_fk_accounts_;
ALTER TABLE IF EXISTS ONLY public.accounts_user_user_permissions DROP CONSTRAINT IF EXISTS accounts_user_user_p_permission_id_113bb443_fk_auth_perm;
ALTER TABLE IF EXISTS ONLY public.accounts_user_groups DROP CONSTRAINT IF EXISTS accounts_user_groups_user_id_52b62117_fk_accounts_user_id;
ALTER TABLE IF EXISTS ONLY public.accounts_user_groups DROP CONSTRAINT IF EXISTS accounts_user_groups_group_id_bd11a704_fk_auth_group_id;
DROP INDEX IF EXISTS public.orders_order_user_id_e9b59eb1;
DROP INDEX IF EXISTS public.orders_order_status_445594e5_like;
DROP INDEX IF EXISTS public.orders_order_status_445594e5;
DROP INDEX IF EXISTS public.orders_order_package_id_9cdaef81;
DROP INDEX IF EXISTS public.orders_order_external_order_id_bd8caaa5_like;
DROP INDEX IF EXISTS public.orders_order_external_order_id_bd8caaa5;
DROP INDEX IF EXISTS public.orders_orde_user_id_02a211_idx;
DROP INDEX IF EXISTS public.esims_esim_user_id_fa9b9a_idx;
DROP INDEX IF EXISTS public.esims_esim_user_id_bc77185c;
DROP INDEX IF EXISTS public.esims_esim_status_5aaa2fce_like;
DROP INDEX IF EXISTS public.esims_esim_status_5aaa2fce;
DROP INDEX IF EXISTS public.esims_esim_order_id_edcc2d53;
DROP INDEX IF EXISTS public.esims_esim_iccid_8df1ebf6_like;
DROP INDEX IF EXISTS public.django_session_session_key_c0390e0f_like;
DROP INDEX IF EXISTS public.django_session_expire_date_a5c62663;
DROP INDEX IF EXISTS public.django_admin_log_user_id_c564eba6;
DROP INDEX IF EXISTS public.django_admin_log_content_type_id_c4bce8eb;
DROP INDEX IF EXISTS public.catalog_package_location_id_28aed20c;
DROP INDEX IF EXISTS public.catalog_package_is_active_d91f555a;
DROP INDEX IF EXISTS public.catalog_package_external_id_c1c28d8a_like;
DROP INDEX IF EXISTS public.catalog_package_country_code_6bb3a144_like;
DROP INDEX IF EXISTS public.catalog_package_country_code_6bb3a144;
DROP INDEX IF EXISTS public.catalog_pac_is_acti_0a8f9d_idx;
DROP INDEX IF EXISTS public.catalog_location_slug_952df9e3_like;
DROP INDEX IF EXISTS public.catalog_location_is_popular_dca9d602;
DROP INDEX IF EXISTS public.catalog_location_coverage_type_c2eabf53_like;
DROP INDEX IF EXISTS public.catalog_location_coverage_type_c2eabf53;
DROP INDEX IF EXISTS public.catalog_location_country_code_c35b2821_like;
DROP INDEX IF EXISTS public.catalog_location_country_code_c35b2821;
DROP INDEX IF EXISTS public.catalog_loc_coverage_pop_idx;
DROP INDEX IF EXISTS public.auth_permission_content_type_id_2f476e4b;
DROP INDEX IF EXISTS public.auth_group_permissions_permission_id_84c5c92e;
DROP INDEX IF EXISTS public.auth_group_permissions_group_id_b120cbf9;
DROP INDEX IF EXISTS public.auth_group_name_a6ea08ec_like;
DROP INDEX IF EXISTS public.accounts_user_user_permissions_user_id_e4f0a161;
DROP INDEX IF EXISTS public.accounts_user_user_permissions_permission_id_113bb443;
DROP INDEX IF EXISTS public.accounts_user_groups_user_id_52b62117;
DROP INDEX IF EXISTS public.accounts_user_groups_group_id_bd11a704;
DROP INDEX IF EXISTS public.accounts_user_email_b2644a56_like;
ALTER TABLE IF EXISTS ONLY public.orders_order DROP CONSTRAINT IF EXISTS orders_order_pkey;
ALTER TABLE IF EXISTS ONLY public.esims_esim DROP CONSTRAINT IF EXISTS esims_esim_pkey;
ALTER TABLE IF EXISTS ONLY public.esims_esim DROP CONSTRAINT IF EXISTS esims_esim_iccid_key;
ALTER TABLE IF EXISTS ONLY public.django_session DROP CONSTRAINT IF EXISTS django_session_pkey;
ALTER TABLE IF EXISTS ONLY public.django_migrations DROP CONSTRAINT IF EXISTS django_migrations_pkey;
ALTER TABLE IF EXISTS ONLY public.django_content_type DROP CONSTRAINT IF EXISTS django_content_type_pkey;
ALTER TABLE IF EXISTS ONLY public.django_content_type DROP CONSTRAINT IF EXISTS django_content_type_app_label_model_76bd3d3b_uniq;
ALTER TABLE IF EXISTS ONLY public.django_admin_log DROP CONSTRAINT IF EXISTS django_admin_log_pkey;
ALTER TABLE IF EXISTS ONLY public.catalog_package DROP CONSTRAINT IF EXISTS catalog_package_pkey;
ALTER TABLE IF EXISTS ONLY public.catalog_package DROP CONSTRAINT IF EXISTS catalog_package_external_id_key;
ALTER TABLE IF EXISTS ONLY public.catalog_location DROP CONSTRAINT IF EXISTS catalog_location_slug_key;
ALTER TABLE IF EXISTS ONLY public.catalog_location DROP CONSTRAINT IF EXISTS catalog_location_pkey;
ALTER TABLE IF EXISTS ONLY public.auth_permission DROP CONSTRAINT IF EXISTS auth_permission_pkey;
ALTER TABLE IF EXISTS ONLY public.auth_permission DROP CONSTRAINT IF EXISTS auth_permission_content_type_id_codename_01ab375a_uniq;
ALTER TABLE IF EXISTS ONLY public.auth_group DROP CONSTRAINT IF EXISTS auth_group_pkey;
ALTER TABLE IF EXISTS ONLY public.auth_group_permissions DROP CONSTRAINT IF EXISTS auth_group_permissions_pkey;
ALTER TABLE IF EXISTS ONLY public.auth_group_permissions DROP CONSTRAINT IF EXISTS auth_group_permissions_group_id_permission_id_0cd325b0_uniq;
ALTER TABLE IF EXISTS ONLY public.auth_group DROP CONSTRAINT IF EXISTS auth_group_name_key;
ALTER TABLE IF EXISTS ONLY public.accounts_user_user_permissions DROP CONSTRAINT IF EXISTS accounts_user_user_permissions_pkey;
ALTER TABLE IF EXISTS ONLY public.accounts_user_user_permissions DROP CONSTRAINT IF EXISTS accounts_user_user_permi_user_id_permission_id_2ab516c2_uniq;
ALTER TABLE IF EXISTS ONLY public.accounts_user DROP CONSTRAINT IF EXISTS accounts_user_pkey;
ALTER TABLE IF EXISTS ONLY public.accounts_user_groups DROP CONSTRAINT IF EXISTS accounts_user_groups_user_id_group_id_59c0b32f_uniq;
ALTER TABLE IF EXISTS ONLY public.accounts_user_groups DROP CONSTRAINT IF EXISTS accounts_user_groups_pkey;
ALTER TABLE IF EXISTS ONLY public.accounts_user DROP CONSTRAINT IF EXISTS accounts_user_email_key;
DROP TABLE IF EXISTS public.orders_order;
DROP TABLE IF EXISTS public.esims_esim;
DROP TABLE IF EXISTS public.django_session;
DROP TABLE IF EXISTS public.django_migrations;
DROP TABLE IF EXISTS public.django_content_type;
DROP TABLE IF EXISTS public.django_admin_log;
DROP TABLE IF EXISTS public.catalog_package;
DROP TABLE IF EXISTS public.catalog_location;
DROP TABLE IF EXISTS public.auth_permission;
DROP TABLE IF EXISTS public.auth_group_permissions;
DROP TABLE IF EXISTS public.auth_group;
DROP TABLE IF EXISTS public.accounts_user_user_permissions;
DROP TABLE IF EXISTS public.accounts_user_groups;
DROP TABLE IF EXISTS public.accounts_user;
SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: accounts_user; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.accounts_user (
    id bigint NOT NULL,
    password character varying(128) NOT NULL,
    last_login timestamp with time zone,
    is_superuser boolean NOT NULL,
    email character varying(254) NOT NULL,
    is_staff boolean NOT NULL,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: accounts_user_groups; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.accounts_user_groups (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    group_id integer NOT NULL
);


--
-- Name: accounts_user_groups_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.accounts_user_groups ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.accounts_user_groups_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: accounts_user_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.accounts_user ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.accounts_user_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: accounts_user_user_permissions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.accounts_user_user_permissions (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    permission_id integer NOT NULL
);


--
-- Name: accounts_user_user_permissions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.accounts_user_user_permissions ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.accounts_user_user_permissions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: auth_group; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.auth_group (
    id integer NOT NULL,
    name character varying(150) NOT NULL
);


--
-- Name: auth_group_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.auth_group ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.auth_group_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: auth_group_permissions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.auth_group_permissions (
    id bigint NOT NULL,
    group_id integer NOT NULL,
    permission_id integer NOT NULL
);


--
-- Name: auth_group_permissions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.auth_group_permissions ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.auth_group_permissions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: auth_permission; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.auth_permission (
    id integer NOT NULL,
    name character varying(255) NOT NULL,
    content_type_id integer NOT NULL,
    codename character varying(100) NOT NULL
);


--
-- Name: auth_permission_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.auth_permission ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.auth_permission_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: catalog_location; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.catalog_location (
    id bigint NOT NULL,
    slug character varying(128) NOT NULL,
    title character varying(255) NOT NULL,
    country_code character varying(2) NOT NULL,
    coverage_type character varying(16) NOT NULL,
    image_url character varying(512) NOT NULL,
    covered_country_codes jsonb NOT NULL,
    is_popular boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    coverages jsonb NOT NULL
);


--
-- Name: catalog_location_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.catalog_location ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.catalog_location_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: catalog_package; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.catalog_package (
    id bigint NOT NULL,
    external_id character varying(64) NOT NULL,
    title character varying(255) NOT NULL,
    operator_title character varying(255) NOT NULL,
    operator_id character varying(64) NOT NULL,
    country_code character varying(2) NOT NULL,
    data_allowance character varying(64) NOT NULL,
    validity_days integer NOT NULL,
    price_usd numeric(10,2) NOT NULL,
    net_price_usd numeric(10,2),
    is_unlimited boolean NOT NULL,
    plan_type character varying(32) NOT NULL,
    source character varying(32) NOT NULL,
    is_active boolean NOT NULL,
    synced_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    location_id bigint,
    voice_minutes integer,
    text_sms integer,
    CONSTRAINT catalog_package_text_sms_check CHECK ((text_sms >= 0)),
    CONSTRAINT catalog_package_validity_days_check CHECK ((validity_days >= 0)),
    CONSTRAINT catalog_package_voice_minutes_check CHECK ((voice_minutes >= 0))
);


--
-- Name: catalog_package_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.catalog_package ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.catalog_package_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: django_admin_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.django_admin_log (
    id integer NOT NULL,
    action_time timestamp with time zone NOT NULL,
    object_id text,
    object_repr character varying(200) NOT NULL,
    action_flag smallint NOT NULL,
    change_message text NOT NULL,
    content_type_id integer,
    user_id bigint NOT NULL,
    CONSTRAINT django_admin_log_action_flag_check CHECK ((action_flag >= 0))
);


--
-- Name: django_admin_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.django_admin_log ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.django_admin_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: django_content_type; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.django_content_type (
    id integer NOT NULL,
    app_label character varying(100) NOT NULL,
    model character varying(100) NOT NULL
);


--
-- Name: django_content_type_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.django_content_type ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.django_content_type_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: django_migrations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.django_migrations (
    id bigint NOT NULL,
    app character varying(255) NOT NULL,
    name character varying(255) NOT NULL,
    applied timestamp with time zone NOT NULL
);


--
-- Name: django_migrations_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.django_migrations ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.django_migrations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: django_session; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.django_session (
    session_key character varying(40) NOT NULL,
    session_data text NOT NULL,
    expire_date timestamp with time zone NOT NULL
);


--
-- Name: esims_esim; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.esims_esim (
    id bigint NOT NULL,
    iccid character varying(32) NOT NULL,
    lpa character varying(255) NOT NULL,
    matching_id character varying(128) NOT NULL,
    qrcode text NOT NULL,
    qrcode_url character varying(512) NOT NULL,
    direct_apple_installation_url character varying(1024) NOT NULL,
    manual_installation text NOT NULL,
    qrcode_installation text NOT NULL,
    installation_guide_url character varying(512) NOT NULL,
    status character varying(32) NOT NULL,
    usage_remaining_mb integer,
    usage_total_mb integer,
    usage_status character varying(64) NOT NULL,
    usage_is_unlimited boolean,
    usage_expired_at timestamp with time zone,
    usage_synced_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    order_id bigint NOT NULL,
    user_id bigint NOT NULL
);


--
-- Name: esims_esim_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.esims_esim ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.esims_esim_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: orders_order; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.orders_order (
    id bigint NOT NULL,
    status character varying(32) NOT NULL,
    external_order_id character varying(64) NOT NULL,
    customer_ref character varying(255) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    package_id bigint NOT NULL,
    user_id bigint NOT NULL
);


--
-- Name: orders_order_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.orders_order ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.orders_order_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Data for Name: accounts_user; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.accounts_user (id, password, last_login, is_superuser, email, is_staff, is_active, created_at, updated_at) FROM stdin;
1	pbkdf2_sha256$870000$UW0IBMbbna8dR8UNt4l6x0$GsB5cAJ9xD4PWpuYUKNZv3GRTLg5KJYtu5U+iXqVGdM=	\N	f	alice@example.com	f	t	2026-07-25 09:51:38.197062+00	2026-07-25 09:51:38.197062+00
2	pbkdf2_sha256$870000$UW0IBMbbna8dR8UNt4l6x0$GsB5cAJ9xD4PWpuYUKNZv3GRTLg5KJYtu5U+iXqVGdM=	\N	f	bob@example.com	f	t	2026-07-25 09:51:38.197062+00	2026-07-25 09:51:38.197062+00
3	pbkdf2_sha256$870000$UW0IBMbbna8dR8UNt4l6x0$GsB5cAJ9xD4PWpuYUKNZv3GRTLg5KJYtu5U+iXqVGdM=	\N	f	inactive@example.com	f	f	2026-07-25 09:51:38.197062+00	2026-07-25 09:51:38.197062+00
\.


--
-- Data for Name: accounts_user_groups; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.accounts_user_groups (id, user_id, group_id) FROM stdin;
\.


--
-- Data for Name: accounts_user_user_permissions; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.accounts_user_user_permissions (id, user_id, permission_id) FROM stdin;
\.


--
-- Data for Name: auth_group; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.auth_group (id, name) FROM stdin;
\.


--
-- Data for Name: auth_group_permissions; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.auth_group_permissions (id, group_id, permission_id) FROM stdin;
\.


--
-- Data for Name: auth_permission; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.auth_permission (id, name, content_type_id, codename) FROM stdin;
1	Can add permission	2	add_permission
2	Can change permission	2	change_permission
3	Can delete permission	2	delete_permission
4	Can view permission	2	view_permission
5	Can add group	3	add_group
6	Can change group	3	change_group
7	Can delete group	3	delete_group
8	Can view group	3	view_group
9	Can add content type	1	add_contenttype
10	Can change content type	1	change_contenttype
11	Can delete content type	1	delete_contenttype
12	Can view content type	1	view_contenttype
13	Can add user	4	add_user
14	Can change user	4	change_user
15	Can delete user	4	delete_user
16	Can view user	4	view_user
17	Can add log entry	5	add_logentry
18	Can change log entry	5	change_logentry
19	Can delete log entry	5	delete_logentry
20	Can view log entry	5	view_logentry
21	Can add session	6	add_session
22	Can change session	6	change_session
23	Can delete session	6	delete_session
24	Can view session	6	view_session
25	Can add package	7	add_package
26	Can change package	7	change_package
27	Can delete package	7	delete_package
28	Can view package	7	view_package
29	Can add location	8	add_location
30	Can change location	8	change_location
31	Can delete location	8	delete_location
32	Can view location	8	view_location
33	Can add order	9	add_order
34	Can change order	9	change_order
35	Can delete order	9	delete_order
36	Can view order	9	view_order
37	Can add esim	10	add_esim
38	Can change esim	10	change_esim
39	Can delete esim	10	delete_esim
40	Can view esim	10	view_esim
\.


--
-- Data for Name: catalog_location; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.catalog_location (id, slug, title, country_code, coverage_type, image_url, covered_country_codes, is_popular, created_at, updated_at, coverages) FROM stdin;
\.


--
-- Data for Name: catalog_package; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.catalog_package (id, external_id, title, operator_title, operator_id, country_code, data_allowance, validity_days, price_usd, net_price_usd, is_unlimited, plan_type, source, is_active, synced_at, created_at, updated_at, location_id, voice_minutes, text_sms) FROM stdin;
1	pkg-us-1gb	1 GB - 7 Days	Change		US	1 GB	7	11.50	10.00	f	data	airalo	t	2026-07-25 09:51:38.197062+00	2026-07-25 09:51:38.197062+00	2026-07-25 09:51:38.197062+00	\N	\N	\N
\.


--
-- Data for Name: django_admin_log; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.django_admin_log (id, action_time, object_id, object_repr, action_flag, change_message, content_type_id, user_id) FROM stdin;
\.


--
-- Data for Name: django_content_type; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.django_content_type (id, app_label, model) FROM stdin;
1	contenttypes	contenttype
2	auth	permission
3	auth	group
4	accounts	user
5	admin	logentry
6	sessions	session
7	catalog	package
8	catalog	location
9	orders	order
10	esims	esim
\.


--
-- Data for Name: django_migrations; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.django_migrations (id, app, name, applied) FROM stdin;
1	contenttypes	0001_initial	2026-07-25 09:51:28.481833+00
2	contenttypes	0002_remove_content_type_name	2026-07-25 09:51:28.485732+00
3	auth	0001_initial	2026-07-25 09:51:29.651255+00
4	auth	0002_alter_permission_name_max_length	2026-07-25 09:51:29.654328+00
5	auth	0003_alter_user_email_max_length	2026-07-25 09:51:29.657123+00
6	auth	0004_alter_user_username_opts	2026-07-25 09:51:29.661402+00
7	auth	0005_alter_user_last_login_null	2026-07-25 09:51:29.664785+00
8	auth	0006_require_contenttypes_0002	2026-07-25 09:51:29.665669+00
9	auth	0007_alter_validators_add_error_messages	2026-07-25 09:51:29.669206+00
10	auth	0008_alter_user_username_max_length	2026-07-25 09:51:29.672216+00
11	auth	0009_alter_user_last_name_max_length	2026-07-25 09:51:29.675648+00
12	auth	0010_alter_group_name_max_length	2026-07-25 09:51:29.679785+00
13	auth	0011_update_proxy_permissions	2026-07-25 09:51:29.683019+00
14	auth	0012_alter_user_first_name_max_length	2026-07-25 09:51:29.686124+00
15	accounts	0001_initial	2026-07-25 09:51:30.905207+00
16	admin	0001_initial	2026-07-25 09:51:32.200068+00
17	admin	0002_logentry_remove_auto_add	2026-07-25 09:51:32.20564+00
18	admin	0003_logentry_add_action_flag_choices	2026-07-25 09:51:32.210999+00
19	sessions	0001_initial	2026-07-25 09:51:33.424338+00
20	catalog	0001_initial	2026-07-25 09:51:34.636928+00
21	catalog	0002_location_and_package_fk	2026-07-25 09:51:34.646068+00
22	catalog	0003_package_voice_text	2026-07-25 09:51:34.652229+00
23	catalog	0004_location_coverages	2026-07-25 09:51:34.65653+00
24	orders	0001_initial	2026-07-25 09:51:35.845029+00
25	esims	0001_initial	2026-07-25 09:51:37.096004+00
\.


--
-- Data for Name: django_session; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.django_session (session_key, session_data, expire_date) FROM stdin;
\.


--
-- Data for Name: esims_esim; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.esims_esim (id, iccid, lpa, matching_id, qrcode, qrcode_url, direct_apple_installation_url, manual_installation, qrcode_installation, installation_guide_url, status, usage_remaining_mb, usage_total_mb, usage_status, usage_is_unlimited, usage_expired_at, usage_synced_at, created_at, updated_at, order_id, user_id) FROM stdin;
1	891000000000009125	lpa.airalo.com	TEST	LPA:1$lpa.airalo.com$TEST						unused	\N	\N		\N	\N	\N	2026-07-25 09:51:38.197062+00	2026-07-25 09:51:38.197062+00	5	2
\.


--
-- Data for Name: orders_order; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.orders_order (id, status, external_order_id, customer_ref, created_at, updated_at, package_id, user_id) FROM stdin;
1	draft		ref-draft	2026-07-25 09:51:38.197062+00	2026-07-25 09:51:38.197062+00	1	1
2	pending_payment		ref-pending	2026-07-25 09:51:38.197062+00	2026-07-25 09:51:38.197062+00	1	1
3	paid	ext-paid-1	ref-paid	2026-07-25 09:51:38.197062+00	2026-07-25 09:51:38.197062+00	1	1
4	fulfilling	ext-fulfilling-1	ref-fulfilling	2026-07-25 09:51:38.197062+00	2026-07-25 09:51:38.197062+00	1	2
5	fulfilled	ext-fulfilled-1	ref-fulfilled	2026-07-25 09:51:38.197062+00	2026-07-25 09:51:38.197062+00	1	2
6	failed	ext-failed-1	ref-failed	2026-07-25 09:51:38.197062+00	2026-07-25 09:51:38.197062+00	1	2
7	cancelled		ref-cancelled	2026-07-25 09:51:38.197062+00	2026-07-25 09:51:38.197062+00	1	1
\.


--
-- Name: accounts_user_groups_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.accounts_user_groups_id_seq', 1, false);


--
-- Name: accounts_user_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.accounts_user_id_seq', 3, true);


--
-- Name: accounts_user_user_permissions_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.accounts_user_user_permissions_id_seq', 1, false);


--
-- Name: auth_group_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.auth_group_id_seq', 1, false);


--
-- Name: auth_group_permissions_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.auth_group_permissions_id_seq', 1, false);


--
-- Name: auth_permission_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.auth_permission_id_seq', 40, true);


--
-- Name: catalog_location_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.catalog_location_id_seq', 1, false);


--
-- Name: catalog_package_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.catalog_package_id_seq', 1, true);


--
-- Name: django_admin_log_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.django_admin_log_id_seq', 1, false);


--
-- Name: django_content_type_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.django_content_type_id_seq', 10, true);


--
-- Name: django_migrations_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.django_migrations_id_seq', 25, true);


--
-- Name: esims_esim_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.esims_esim_id_seq', 1, true);


--
-- Name: orders_order_id_seq; Type: SEQUENCE SET; Schema: public; Owner: -
--

SELECT pg_catalog.setval('public.orders_order_id_seq', 7, true);


--
-- Name: accounts_user accounts_user_email_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounts_user
    ADD CONSTRAINT accounts_user_email_key UNIQUE (email);


--
-- Name: accounts_user_groups accounts_user_groups_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounts_user_groups
    ADD CONSTRAINT accounts_user_groups_pkey PRIMARY KEY (id);


--
-- Name: accounts_user_groups accounts_user_groups_user_id_group_id_59c0b32f_uniq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounts_user_groups
    ADD CONSTRAINT accounts_user_groups_user_id_group_id_59c0b32f_uniq UNIQUE (user_id, group_id);


--
-- Name: accounts_user accounts_user_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounts_user
    ADD CONSTRAINT accounts_user_pkey PRIMARY KEY (id);


--
-- Name: accounts_user_user_permissions accounts_user_user_permi_user_id_permission_id_2ab516c2_uniq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounts_user_user_permissions
    ADD CONSTRAINT accounts_user_user_permi_user_id_permission_id_2ab516c2_uniq UNIQUE (user_id, permission_id);


--
-- Name: accounts_user_user_permissions accounts_user_user_permissions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounts_user_user_permissions
    ADD CONSTRAINT accounts_user_user_permissions_pkey PRIMARY KEY (id);


--
-- Name: auth_group auth_group_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_group
    ADD CONSTRAINT auth_group_name_key UNIQUE (name);


--
-- Name: auth_group_permissions auth_group_permissions_group_id_permission_id_0cd325b0_uniq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_group_permissions
    ADD CONSTRAINT auth_group_permissions_group_id_permission_id_0cd325b0_uniq UNIQUE (group_id, permission_id);


--
-- Name: auth_group_permissions auth_group_permissions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_group_permissions
    ADD CONSTRAINT auth_group_permissions_pkey PRIMARY KEY (id);


--
-- Name: auth_group auth_group_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_group
    ADD CONSTRAINT auth_group_pkey PRIMARY KEY (id);


--
-- Name: auth_permission auth_permission_content_type_id_codename_01ab375a_uniq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_permission
    ADD CONSTRAINT auth_permission_content_type_id_codename_01ab375a_uniq UNIQUE (content_type_id, codename);


--
-- Name: auth_permission auth_permission_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_permission
    ADD CONSTRAINT auth_permission_pkey PRIMARY KEY (id);


--
-- Name: catalog_location catalog_location_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.catalog_location
    ADD CONSTRAINT catalog_location_pkey PRIMARY KEY (id);


--
-- Name: catalog_location catalog_location_slug_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.catalog_location
    ADD CONSTRAINT catalog_location_slug_key UNIQUE (slug);


--
-- Name: catalog_package catalog_package_external_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.catalog_package
    ADD CONSTRAINT catalog_package_external_id_key UNIQUE (external_id);


--
-- Name: catalog_package catalog_package_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.catalog_package
    ADD CONSTRAINT catalog_package_pkey PRIMARY KEY (id);


--
-- Name: django_admin_log django_admin_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.django_admin_log
    ADD CONSTRAINT django_admin_log_pkey PRIMARY KEY (id);


--
-- Name: django_content_type django_content_type_app_label_model_76bd3d3b_uniq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.django_content_type
    ADD CONSTRAINT django_content_type_app_label_model_76bd3d3b_uniq UNIQUE (app_label, model);


--
-- Name: django_content_type django_content_type_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.django_content_type
    ADD CONSTRAINT django_content_type_pkey PRIMARY KEY (id);


--
-- Name: django_migrations django_migrations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.django_migrations
    ADD CONSTRAINT django_migrations_pkey PRIMARY KEY (id);


--
-- Name: django_session django_session_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.django_session
    ADD CONSTRAINT django_session_pkey PRIMARY KEY (session_key);


--
-- Name: esims_esim esims_esim_iccid_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.esims_esim
    ADD CONSTRAINT esims_esim_iccid_key UNIQUE (iccid);


--
-- Name: esims_esim esims_esim_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.esims_esim
    ADD CONSTRAINT esims_esim_pkey PRIMARY KEY (id);


--
-- Name: orders_order orders_order_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orders_order
    ADD CONSTRAINT orders_order_pkey PRIMARY KEY (id);


--
-- Name: accounts_user_email_b2644a56_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX accounts_user_email_b2644a56_like ON public.accounts_user USING btree (email varchar_pattern_ops);


--
-- Name: accounts_user_groups_group_id_bd11a704; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX accounts_user_groups_group_id_bd11a704 ON public.accounts_user_groups USING btree (group_id);


--
-- Name: accounts_user_groups_user_id_52b62117; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX accounts_user_groups_user_id_52b62117 ON public.accounts_user_groups USING btree (user_id);


--
-- Name: accounts_user_user_permissions_permission_id_113bb443; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX accounts_user_user_permissions_permission_id_113bb443 ON public.accounts_user_user_permissions USING btree (permission_id);


--
-- Name: accounts_user_user_permissions_user_id_e4f0a161; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX accounts_user_user_permissions_user_id_e4f0a161 ON public.accounts_user_user_permissions USING btree (user_id);


--
-- Name: auth_group_name_a6ea08ec_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX auth_group_name_a6ea08ec_like ON public.auth_group USING btree (name varchar_pattern_ops);


--
-- Name: auth_group_permissions_group_id_b120cbf9; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX auth_group_permissions_group_id_b120cbf9 ON public.auth_group_permissions USING btree (group_id);


--
-- Name: auth_group_permissions_permission_id_84c5c92e; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX auth_group_permissions_permission_id_84c5c92e ON public.auth_group_permissions USING btree (permission_id);


--
-- Name: auth_permission_content_type_id_2f476e4b; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX auth_permission_content_type_id_2f476e4b ON public.auth_permission USING btree (content_type_id);


--
-- Name: catalog_loc_coverage_pop_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX catalog_loc_coverage_pop_idx ON public.catalog_location USING btree (coverage_type, is_popular);


--
-- Name: catalog_location_country_code_c35b2821; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX catalog_location_country_code_c35b2821 ON public.catalog_location USING btree (country_code);


--
-- Name: catalog_location_country_code_c35b2821_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX catalog_location_country_code_c35b2821_like ON public.catalog_location USING btree (country_code varchar_pattern_ops);


--
-- Name: catalog_location_coverage_type_c2eabf53; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX catalog_location_coverage_type_c2eabf53 ON public.catalog_location USING btree (coverage_type);


--
-- Name: catalog_location_coverage_type_c2eabf53_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX catalog_location_coverage_type_c2eabf53_like ON public.catalog_location USING btree (coverage_type varchar_pattern_ops);


--
-- Name: catalog_location_is_popular_dca9d602; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX catalog_location_is_popular_dca9d602 ON public.catalog_location USING btree (is_popular);


--
-- Name: catalog_location_slug_952df9e3_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX catalog_location_slug_952df9e3_like ON public.catalog_location USING btree (slug varchar_pattern_ops);


--
-- Name: catalog_pac_is_acti_0a8f9d_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX catalog_pac_is_acti_0a8f9d_idx ON public.catalog_package USING btree (is_active, country_code);


--
-- Name: catalog_package_country_code_6bb3a144; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX catalog_package_country_code_6bb3a144 ON public.catalog_package USING btree (country_code);


--
-- Name: catalog_package_country_code_6bb3a144_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX catalog_package_country_code_6bb3a144_like ON public.catalog_package USING btree (country_code varchar_pattern_ops);


--
-- Name: catalog_package_external_id_c1c28d8a_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX catalog_package_external_id_c1c28d8a_like ON public.catalog_package USING btree (external_id varchar_pattern_ops);


--
-- Name: catalog_package_is_active_d91f555a; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX catalog_package_is_active_d91f555a ON public.catalog_package USING btree (is_active);


--
-- Name: catalog_package_location_id_28aed20c; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX catalog_package_location_id_28aed20c ON public.catalog_package USING btree (location_id);


--
-- Name: django_admin_log_content_type_id_c4bce8eb; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX django_admin_log_content_type_id_c4bce8eb ON public.django_admin_log USING btree (content_type_id);


--
-- Name: django_admin_log_user_id_c564eba6; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX django_admin_log_user_id_c564eba6 ON public.django_admin_log USING btree (user_id);


--
-- Name: django_session_expire_date_a5c62663; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX django_session_expire_date_a5c62663 ON public.django_session USING btree (expire_date);


--
-- Name: django_session_session_key_c0390e0f_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX django_session_session_key_c0390e0f_like ON public.django_session USING btree (session_key varchar_pattern_ops);


--
-- Name: esims_esim_iccid_8df1ebf6_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX esims_esim_iccid_8df1ebf6_like ON public.esims_esim USING btree (iccid varchar_pattern_ops);


--
-- Name: esims_esim_order_id_edcc2d53; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX esims_esim_order_id_edcc2d53 ON public.esims_esim USING btree (order_id);


--
-- Name: esims_esim_status_5aaa2fce; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX esims_esim_status_5aaa2fce ON public.esims_esim USING btree (status);


--
-- Name: esims_esim_status_5aaa2fce_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX esims_esim_status_5aaa2fce_like ON public.esims_esim USING btree (status varchar_pattern_ops);


--
-- Name: esims_esim_user_id_bc77185c; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX esims_esim_user_id_bc77185c ON public.esims_esim USING btree (user_id);


--
-- Name: esims_esim_user_id_fa9b9a_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX esims_esim_user_id_fa9b9a_idx ON public.esims_esim USING btree (user_id, status);


--
-- Name: orders_orde_user_id_02a211_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX orders_orde_user_id_02a211_idx ON public.orders_order USING btree (user_id, status);


--
-- Name: orders_order_external_order_id_bd8caaa5; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX orders_order_external_order_id_bd8caaa5 ON public.orders_order USING btree (external_order_id);


--
-- Name: orders_order_external_order_id_bd8caaa5_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX orders_order_external_order_id_bd8caaa5_like ON public.orders_order USING btree (external_order_id varchar_pattern_ops);


--
-- Name: orders_order_package_id_9cdaef81; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX orders_order_package_id_9cdaef81 ON public.orders_order USING btree (package_id);


--
-- Name: orders_order_status_445594e5; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX orders_order_status_445594e5 ON public.orders_order USING btree (status);


--
-- Name: orders_order_status_445594e5_like; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX orders_order_status_445594e5_like ON public.orders_order USING btree (status varchar_pattern_ops);


--
-- Name: orders_order_user_id_e9b59eb1; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX orders_order_user_id_e9b59eb1 ON public.orders_order USING btree (user_id);


--
-- Name: accounts_user_groups accounts_user_groups_group_id_bd11a704_fk_auth_group_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounts_user_groups
    ADD CONSTRAINT accounts_user_groups_group_id_bd11a704_fk_auth_group_id FOREIGN KEY (group_id) REFERENCES public.auth_group(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: accounts_user_groups accounts_user_groups_user_id_52b62117_fk_accounts_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounts_user_groups
    ADD CONSTRAINT accounts_user_groups_user_id_52b62117_fk_accounts_user_id FOREIGN KEY (user_id) REFERENCES public.accounts_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: accounts_user_user_permissions accounts_user_user_p_permission_id_113bb443_fk_auth_perm; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounts_user_user_permissions
    ADD CONSTRAINT accounts_user_user_p_permission_id_113bb443_fk_auth_perm FOREIGN KEY (permission_id) REFERENCES public.auth_permission(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: accounts_user_user_permissions accounts_user_user_p_user_id_e4f0a161_fk_accounts_; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounts_user_user_permissions
    ADD CONSTRAINT accounts_user_user_p_user_id_e4f0a161_fk_accounts_ FOREIGN KEY (user_id) REFERENCES public.accounts_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: auth_group_permissions auth_group_permissio_permission_id_84c5c92e_fk_auth_perm; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_group_permissions
    ADD CONSTRAINT auth_group_permissio_permission_id_84c5c92e_fk_auth_perm FOREIGN KEY (permission_id) REFERENCES public.auth_permission(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: auth_group_permissions auth_group_permissions_group_id_b120cbf9_fk_auth_group_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_group_permissions
    ADD CONSTRAINT auth_group_permissions_group_id_b120cbf9_fk_auth_group_id FOREIGN KEY (group_id) REFERENCES public.auth_group(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: auth_permission auth_permission_content_type_id_2f476e4b_fk_django_co; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_permission
    ADD CONSTRAINT auth_permission_content_type_id_2f476e4b_fk_django_co FOREIGN KEY (content_type_id) REFERENCES public.django_content_type(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: catalog_package catalog_package_location_id_28aed20c_fk_catalog_location_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.catalog_package
    ADD CONSTRAINT catalog_package_location_id_28aed20c_fk_catalog_location_id FOREIGN KEY (location_id) REFERENCES public.catalog_location(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: django_admin_log django_admin_log_content_type_id_c4bce8eb_fk_django_co; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.django_admin_log
    ADD CONSTRAINT django_admin_log_content_type_id_c4bce8eb_fk_django_co FOREIGN KEY (content_type_id) REFERENCES public.django_content_type(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: django_admin_log django_admin_log_user_id_c564eba6_fk_accounts_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.django_admin_log
    ADD CONSTRAINT django_admin_log_user_id_c564eba6_fk_accounts_user_id FOREIGN KEY (user_id) REFERENCES public.accounts_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: esims_esim esims_esim_order_id_edcc2d53_fk_orders_order_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.esims_esim
    ADD CONSTRAINT esims_esim_order_id_edcc2d53_fk_orders_order_id FOREIGN KEY (order_id) REFERENCES public.orders_order(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: esims_esim esims_esim_user_id_bc77185c_fk_accounts_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.esims_esim
    ADD CONSTRAINT esims_esim_user_id_bc77185c_fk_accounts_user_id FOREIGN KEY (user_id) REFERENCES public.accounts_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: orders_order orders_order_package_id_9cdaef81_fk_catalog_package_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orders_order
    ADD CONSTRAINT orders_order_package_id_9cdaef81_fk_catalog_package_id FOREIGN KEY (package_id) REFERENCES public.catalog_package(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: orders_order orders_order_user_id_e9b59eb1_fk_accounts_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orders_order
    ADD CONSTRAINT orders_order_user_id_e9b59eb1_fk_accounts_user_id FOREIGN KEY (user_id) REFERENCES public.accounts_user(id) DEFERRABLE INITIALLY DEFERRED;


--
-- PostgreSQL database dump complete
--

