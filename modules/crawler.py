import argparse
import datetime
import hashlib
import json
import logging
import os
import re
import socket
import ssl
import subprocess
import time
import traceback
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

import blockcypher
import feedparser
import imagehash
import pandas as pd
import requests
import whois
import yaml
from bs4 import BeautifulSoup
from OpenSSL import crypto
from PIL import Image
from tldextract import tldextract
from usp.tree import sitemap_tree_for_homepage

from modules.indicator import Indicator
from modules.indicators import (EMBEDDED_IDS, FINANCIAL_IDS, SOCIAL_MEDIA_IDS,
                                TRACKING_IDS)

URLSCAN_API_KEY = os.getenv('URLSCAN_API_KEY', '')
SCRAPER_API_KEY = os.getenv('SCRAPER_API_KEY', '')
MYIPMS_API_PATH = os.getenv('MYIPMS_API_PATH', '')

visited = set()


def valid_url(url):
    # Regular expression for matching a URL
    regex = r"^(?:http|ftp)s?://"

    # If the URL does not match the regular expression
    if not re.match(regex, url):
        url = url.strip("/")
        # Add the 'http://' prefix to the URL
        url = "http://" + url
    if url == "http://":
        return ""

    return url


def get_domain_name(url) -> str:
    domain_extract = tldextract.extract(url)
    sd = domain_extract.subdomain
    d = domain_extract.domain
    su = domain_extract.suffix
    if not sd or sd == 'www':
        return f"{d}.{su}"
    else:
        return f"{sd}.{d}.{su}"


def add_response_headers(response) -> list[Indicator]:
    header_indicators = []
    if not response.headers:
        return header_indicators
    for header, value in response.headers.items():
        try:
            if header.startswith("Server"):
                header_indicators.append(Indicator("3-header-server", value))
            if (
                header.startswith("X-") or header.startswith("x-")
            ) and header.lower() not in [
                "x-content-type-options",
                "x-frame-options",
                "x-xss-protection",
                "x-request-id",
                "x-ua-compatible",
                "x-permitted-cross-domain-policies",
                "x-dns-prefetch-control",
                "x-robots-tag",
            ]:
                header_indicators.append(
                    Indicator("3-header-nonstd-value", header + ":" + value)
                )
        except Exception as e:
            logging.error(e)

    return header_indicators


def add_ip_address(domain_name) -> list[Indicator]:
    ip_indicators = []
    if domain_name.startswith("https://"):
        host_name = domain_name[8:]
    else:
        host_name = domain_name

    try:
        # Resolve the domain name to an IP address
        ip_address = socket.gethostbyname(host_name)
        ip_indicators.append(Indicator("1-ip", ip_address))

        last_period_index = ip_address.rfind(".")
        subnet_id = ip_address[:last_period_index]
        ip_indicators.append(Indicator("2-subnet", subnet_id))
    except socket.gaierror:
        logging.error("Could not resolve the domain name {}".format(domain_name))
    finally:
        return ip_indicators


def add_who_is(url) -> list[Indicator]:
    whois_indicators = []
    try:
        result = whois.whois(url)
        if result.text != "Socket not responding: [Errno 11001] getaddrinfo failed":
            whois_indicators.append(Indicator("3-whois-registrar", result.registrar))
            whois_indicators.append(Indicator("3-whois_server", result.whois_server))
            whois_indicators.append(Indicator("3-whois_creation_date", result.creation_date))
            if (
                "name" in result
                and result.name is not None
                and isinstance(result.name, str)
            ):
                if (
                    "priva" not in result.name.lower()
                    and "proxy" not in result.name.lower()
                    and "guard" not in result.name.lower()
                    and "protect" not in result.name.lower()
                    and "mask" not in result.name.lower()
                    and "secur" not in result.name.lower()
                ):
                    whois_indicators.append(Indicator("1-whois_emails", result.emails))
                    whois_indicators.append(Indicator("1-whois_name", result.name))
                    whois_indicators.append(Indicator("1-whois_org", result.org))
                    whois_indicators.append(Indicator("1-whois_address", result.address))
                    whois_indicators.append(
                        Indicator("2-whois_citystatecountry", f"{result.city}, {result.state}, {result.country}")
                    )
    except Exception as e:
        traceback.print_exc()

    return whois_indicators


def get_tracert(ip_address):
    tracert = subprocess.Popen(["tracert", ip_address], stdout=subprocess.PIPE)
    output, _ = tracert.communicate()
    return output.decode().strip().split("\n")


def parse_classes(soup) -> list[Indicator]:
    tag_indicators = []
    used_classes = set()
    for elem in soup.select("[class]"):
        classes = elem["class"]
        used_classes.update(classes)
    tag_indicators.append(Indicator("3-css_classes", list(used_classes)))
    return tag_indicators


def parse_sitemaps(url) -> list[Indicator]:
    tag_indicators = []
    tree = sitemap_tree_for_homepage(url)
    logging.info(tree)
    entries = set()
    for page in tree.all_pages():
        entries.update(page.url)
    tag_indicators.append(Indicator("4-sitemap_entries", entries))
    return tag_indicators


def parse_dom_tree(soup) -> list[Indicator]:
    tag_indicators = []
    for text in soup.find_all(text=True):
        text.replace_with("")
    for tag in soup.find_all():
        tag.attrs = {}
    tag_indicators.append(Indicator("3-dom_tree", soup.prettify()))
    return tag_indicators


def parse_images(url, soup, response) -> list[Indicator]:
    tag_indicators = []
    image_links = []
    for img in soup.find_all("img"):
        if img.has_attr("src") and img["src"].startswith("/"):
            image_links.append(url + img["src"])
        elif img.has_attr("src"):
            image_links.append(img["src"])
    for link in image_links:
        try:
            response = requests.get(link)
            img = Image.open(BytesIO(response.content))
            image_hash = imagehash.phash(img)
            tag_indicators.append(Indicator("3-image-phash", image_hash))
        except Exception as ex:
            continue  # logging.error(ex.message)

    return tag_indicators


def parse_meta_tags(soup) -> list[Indicator]:
    meta_tags = soup.find_all("meta")
    tag_indicators = []
    generic_metas = []
    # Iterate over the meta tags
    for meta_tag in meta_tags:
        # Get the name and content attributes of the meta tags
        name = meta_tag.get("name")
        prop = meta_tag.get("property")
        content = meta_tag.get("content")
        if name and "verif" in name.lower():
            tag_indicators.append(Indicator("1-verification_id", name + "|" + content))
        elif name and name in ["twitter:site", "fb:pages"]:
            tag_indicators.append(Indicator("3-meta_social", name + "|" + content))
        elif (name or prop) and content:
            name = name or prop
            generic_metas.append(name + "|" + content)
    tag_indicators.append(Indicator("3-meta_generic", generic_metas))
    return tag_indicators


def parse_script_tags(url, soup) -> list[Indicator]:
    script_tags = soup.find_all("script")
    tag_indicators = []
    script_tags = []
    # Iterate over the meta tags
    for script_tag in script_tags:
        # Get the name and content attributes of the meta tags
        source = script_tag.get("src")
        if source:
            match = re.search(r"/([^/]+)$", source)
            if match and match.group(1) not in script_tags:
                script_tags.append(match.group(1))
    tag_indicators = [Indicator("3-script_src", script_tags)]
    return tag_indicators


def parse_id_attributes(soup) -> list[Indicator]:
    ids = [element["id"] for element in soup.find_all(id=True)]
    id_indicators = [Indicator("3-id_tags", ids)]
    return id_indicators


def parse_iframe_ids(soup) -> list[Indicator]:
    iframe_ids = [
        iframe["id"] for iframe in soup.find_all("iframe") if "id" in iframe.attrs
    ]
    iframe_indicators = []
    for iframe in iframe_ids:
        iframe_indicators.append(Indicator("3-iframe_id_tags", iframe))
    return iframe_indicators


def parse_link_tags(url, soup) -> list[Indicator]:
    link_tags = soup.find_all("link")
    href_links = [link["href"] for link in link_tags if link.has_attr("href")]
    tag_indicators = []
    # Iterate over the link tags

    tag_indicators.append(Indicator("3-link_href", href_links))

    return tag_indicators


def bulk_builtwith_query(domains: list[str], save_matches: bool = False) -> list[Indicator]:
    api_keys = yaml.safe_load(open("config/api_keys.yml", "r"))
    builtwith_key = api_keys.get("BUILT_WITH")
    if not builtwith_key:
        logging.warn("No Builtwith API key provided. Skipping.")
        return []
    techstack_indicators = get_techstack_indicators(
        domains=domains, api_key=builtwith_key
    )
    techidentifier_indicators = get_tech_identifiers(
        domains=domains, api_key=builtwith_key, save_matches=save_matches
    )
    return techstack_indicators + techidentifier_indicators


def get_techstack_indicators(domains: list[str], api_key: str) -> list[Indicator]:
    domain_list = ",".join(domains)
    tech_stack_query = (
        f"https://api.builtwith.com/v20/api.json?KEY={api_key}&LOOKUP={domain_list}"
    )
    try:
        api_result = requests.get(tech_stack_query)
        data = json.loads(api_result.content)
        # API supports querying multiple sites at a time, hence the embedded structure
        for result_item in data["Results"]:
            result = result_item["Result"]
            tech_stack = []
            for path in result["Paths"]:
                technologies = [
                    Indicator("techstack",{
                            "name": tech.get("Name"),
                            "link": tech.get("Link"),
                            "tag": tech.get("Tag"),
                            "subdomain": path["SubDomain"],
                        })
                    for tech in path["Technologies"]
                ]
                tech_stack.extend(technologies)
            return tech_stack
    except IndexError:
        logging.warn(
            "Error hit iterating through results. Have you hit your Builtwith API limit?"
        )
        traceback.print_exc()
    except Exception:
        traceback.print_exc()
    finally:
        return []


def get_tech_identifiers(domains: list[str], api_key: str, save_matches: bool = False) -> list[Indicator]:
    domain_list = ",".join(domains)
    tech_relation_query = (
        f"https://api.builtwith.com/rv2/api.json?KEY={api_key}&LOOKUP={domain_list}"
    )
    api_result = requests.get(tech_relation_query)

    try:
        data = json.loads(api_result.content)
        for result_item in data["Relationships"]:
            relations = result_item["Identifiers"]
            matches_df = (
                pd.DataFrame(relations).explode("Matches").rename(columns=str.lower)
            )
            if save_matches:
                matches_df.to_csv(f"{domain}_identifier_matches.csv")
            identifiers = (
                matches_df.groupby(["type", "value"])["matches"]
                .count()
                .to_frame("num_matches")
                .reset_index()
            )
            # applying indicator structure
            return [Indicator("tech_identifier", identifier) for identifier in identifiers.to_dict(orient="records")]
    except IndexError as e:
        logging.warn(
            "Error hit iterating through results. Have you hit your Builtwith API limit?"
        )
        traceback.print_exc()
    except Exception as e:
        traceback.print_exc()
    finally:
        return []


def fetch_shodan_data(ip):
    url = f"https://internetdb.shodan.io/{ip}"
    response = requests.get(url)
    if response.status_code == 200:
        return response.json()
    else:
        return None


def parse_shodan_json(shodan_json) -> list[Indicator]:
    shodan_indicators = []
    if shodan_json:
        if len(shodan_json["hostnames"]) > 0:
            for hostname in shodan_json["hostnames"]:
                shodan_indicators.append(Indicator("1-ip_shodan_hostnames", hostname))
        if len(shodan_json["vulns"]) > 0:
            shodan_indicators.append(Indicator("2-ip_shodan_vuln", shodan_json["vulns"]))
        if len(shodan_json["cpes"]) > 0:
            shodan_indicators.append(Indicator("3-ip_shodan_cpe", shodan_json["cpes"]))
        if len(shodan_json["ports"]) > 0:
            shodan_indicators.append(Indicator("3-ip_shodan_ports", shodan_json["ports"]))

    return shodan_indicators


def get_shodan_indicators(url) -> list[Indicator]:
    shodan_indicators = []
    domain = get_domain_name(url)
    try:
        ip = socket.gethostbyname(domain)

        shodan_json = fetch_shodan_data(ip)
        shodan_indicators = parse_shodan_json(shodan_json)
    except Exception as e:
        traceback.print_exc()
    finally:
        return shodan_indicators


def get_ipms_indicators(url) -> list[Indicator]:
    ipms_indicators = []
    try:
        if len(MYIPMS_API_PATH) > 0:
            api_url = "https://api.myip.ms"
            domain = get_domain_name(url)
            # Generate the current GMT timestamp
            timestamp = datetime.datetime.utcnow().strftime("%Y-%m-%d_%H:%M:%S")

            # Create the signature
            ipms_domain_signature_raw = (
                f"{api_url}/{domain}/{MYIPMS_API_PATH}/timestamp/{timestamp}"
            )
            ipms_domain_signature = hashlib.md5(
                ipms_domain_signature_raw.encode()
            ).hexdigest()
            # Construct the URL
            ipms_domain_url = f"{api_url}/{domain}/{MYIPMS_API_PATH}/signature/{ipms_domain_signature}/timestamp/{timestamp}"

            # Repeat the process for the IP address
            ip_address = socket.gethostbyname(domain)
            ipms_ip_signature_raw = (
                f"{api_url}/{ip_address}/{MYIPMS_API_PATH}/timestamp/{timestamp}"
            )
            ipms_ip_signature = hashlib.md5(ipms_ip_signature_raw.encode()).hexdigest()
            ipms_ip_url = f"{api_url}/{ip_address}/{MYIPMS_API_PATH}/signature/{ipms_ip_signature}/timestamp/{timestamp}"

            ipms_indicators.extend(get_ipms_domain_indicators(ipms_domain_url))
            ipms_indicators.extend(get_ipms_ip_indicators(ipms_ip_url))
    except Exception as e:
        traceback.print_exc()
    finally:
        return ipms_indicators


def get_ipms_domain_indicators(ipms_url) -> list[Indicator]:
    ipms_indicators = []
    api_result = requests.get(ipms_url)

    try:
        data = json.loads(api_result.content)
        if "owners" in data:
            ipms_indicators.append(
                Indicator(
                    "3-ipms_domain_iprangeowner_cidr", data["owners"]["owner"]["cidr"]
                )
            )
            ipms_indicators.append(
                Indicator(
                    "3-ipms_domain_iprangeowner_ownerName",
                    data["owners"]["owner"]["ownerName"],
                )
            )
            ipms_indicators.append(
                Indicator(
                    "3-ipms_domain_iprangeowner_address",
                    data["owners"]["owner"]["address"],
                )
            )
        for dns in data.get("dns", []):
            ipms_indicators.append(
                Indicator("3-ipms_domain_nameserver", dns["nameserver"])
            )
        unique_ips = {entry["ip_address"] for entry in data.get("ip_change_history", [])}
        for ip in unique_ips:
            ipms_indicators.append(Indicator("3-ipms_domain_otheripused", ip))

        return ipms_indicators

    except IndexError as e:
        logging.warn(
            "Error hit iterating eunning through IPMS results. Have you hit your IPMS API limit?"
        )
        traceback.print_exc()
    except Exception as e:
        traceback.print_exc()
    finally:
        return ipms_indicators


def get_ipms_ip_indicators(ipms_url) -> list[Indicator]:
    ipms_indicators = []
    api_result = requests.get(ipms_url)

    try:
        data = json.loads(api_result.content)
        for site in data.get("websites_on_ip_now", []):
            ipms_indicators.append(
                Indicator("3-ipms_siteonthisip_now", site["website"])
            )
        for site in data.get("websites_on_ip_before", []):
            ipms_indicators.append(
                Indicator("3-ipms_siteonthisip_before", site["website"])
            )
        for site in data.get("not_working_websites_on_ip", []):
            ipms_indicators.append(
                Indicator("3-ipms_siteonthisip_broken", site["website"])
            )
        for useragent in data.get("useragents_on_ip", []):
            ipms_indicators.append(
                Indicator("3-ipms_useragents", useragent["useragent"])
            )

        return ipms_indicators
    except IndexError as e:
        logging.warn(
            "Error hit iterating running through IPMS results. Have you hit your IPMS API limit?"
        )
        traceback.print_exc()
    except Exception as e:
        traceback.print_exc()
    finally:
        return ipms_indicators


def parse_body(url, response) -> list[Indicator]:
    text = response.text
    tag_indicators = []
    tag_indicators.extend(find_uuids(text))
    tag_indicators.extend(find_wallets(text))

    return tag_indicators


def parse_footer(soup) -> list[Indicator]:
    tag_indicators = []

    footer = soup.find("footer")
    # Extract text
    if footer:
        footer_text = footer.get_text(strip=True)
        tag_indicators.append(Indicator("3-footer-text", footer_text))

    return tag_indicators


def find_with_regex(regex, text, indicator_type) -> list[Indicator]:
    tag_indicators = []
    matches = set(re.findall(regex, text))
    for match in matches:
        tag_indicators.append(Indicator(indicator_type, match))
    return tag_indicators


def find_uuids(text):
    uuid_pattern = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    return find_with_regex(uuid_pattern, text, "3-uuid")


def find_wallets(text) -> list[Indicator]:
    tag_indicators = []
    crypto_wallet_pattern = r"[^a-zA-Z0-9](0x[a-fA-F0-9]{40}|[13][a-zA-Z0-9]{24,33}|[4][a-zA-Z0-9]{95}|[qp][a-zA-Z0-9]{25,34})[^a-zA-Z0-9]"

    btc_address_regex = re.compile(r"^[13][a-km-zA-HJ-NP-Z1-9]{25,34}$")
    btc_matches = set(re.findall(btc_address_regex, text))
    # Get transaction data for the address from the BlockCypher API
    for match in btc_matches:
        tag_indicators.append(find_wallet_transactions(wallet=match, wallet_type="btc"))

    bch_address_regex = re.compile(r"^[qQ][a-km-zA-HJ-NP-Z1-9]{41}$")
    bch_matches = set(re.findall(bch_address_regex, text))
    # Get transaction data for the address from the BlockCypher API
    for match in bch_matches:
        tag_indicators.append(find_wallet_transactions(wallet=match, wallet_type="bch"))

    tag_indicators.extend(
        find_with_regex(crypto_wallet_pattern, text, "1-crypto-wallet")
    )
    return tag_indicators


def find_wallet_transactions(wallet_type, wallet) -> list[Indicator]:
    tx_data = blockcypher.get_address_full(wallet, coin_symbol=wallet_type)
    tag_indicators = []

    # Check if transaction data exists for the address
    if tx_data:
        # Extract the addresses involved in transactions with the given address
        addresses = set()
        for input in tx_data["txs"]:
            for address in input["addresses"]:
                addresses.add(address)
        for address in addresses:
            tag_indicators.append(Indicator("2-crypto-transacation", address))
    return tag_indicators


def add_associated_domains_from_cert(url) -> list[Indicator]:
    tag_indicators = []
    try:
        port = 443


        cert = ssl.get_server_certificate((get_domain_name(url), port))
        x509 = crypto.load_certificate(crypto.FILETYPE_PEM, cert)

        sans = []
        for i in range(x509.get_extension_count()):
            ext = x509.get_extension(i)
            if ext.get_short_name() == b"subjectAltName":
                ext_val = ext.__str__()
                sans = ext_val.replace("DNS:", "").split(",")

        for san in sans:
            tag_indicators.append(Indicator("1-cert-domain", san))
    except Exception as e:
        logging.error(f"Error in add_associated_domains_from_cert for {url}. Will continue. Traceback below.")
        traceback.print_exc()
    finally:
        return tag_indicators

def parse_id_patterns(response, id_patterns: dict[str,str]) -> list[Indicator]:
    tag_indicators = []
    for id_type, pattern in id_patterns.items():
        id_indicators = find_with_regex(regex=pattern, text=response.text, indicator_type=id_type)
        tag_indicators.extend(id_indicators)
    return tag_indicators


def add_cdn_domains(soup) -> list[Indicator]:
    tag_indicators = []

    img_tags = soup.find_all("img")
    domains = set()
    for img_tag in img_tags:
        src = img_tag.get("src")
        if src:
            domain = urlsplit(src).hostname
            domains.add(domain)
    for domain in domains:
        tag_indicators.append(Indicator("3-cdn-domain", domain))
    return tag_indicators


# getting domain and suffix, eg -  “google.com”
def find_domain_suffix(url) -> list[Indicator]:
    tag_indicators = []
    ext = tldextract.extract(url)
    domain_suffix = ext.suffix + "." + ext.domain
    tag_indicators.append(Indicator(type="1-domain_suffix", content=domain_suffix))
    return tag_indicators  # joins the strings


def find_second_level_domain(url) -> list[Indicator]:
    tag_indicators = []
    ext = tldextract.extract(url)
    domain = ext.domain
    tag_indicators.append(Indicator(type="1-domain", content=domain))
    return tag_indicators


def parse_domain_name(url) -> list[Indicator]:
    tag_indicators = []
    tag_indicators.extend(find_domain_suffix(url))
    tag_indicators.extend(find_second_level_domain(url))
    return tag_indicators


def start_urlscan(url):
    urlscan_key = URLSCAN_API_KEY
    if not urlscan_key:
        logging.warn("No urlscan API key provided. Passing...")
        return None
    headers = {"API-Key": urlscan_key, "Content-Type": "application/json"}
    data = {"url": url, "visibility": "public"}
    response = requests.post(
        "https://urlscan.io/api/v1/scan/", headers=headers, data=json.dumps(data)
    )
    submission_response = response.json()
    return submission_response["api"]


# todo: add more indicators from urlscan
def add_urlscan_indicators(data) -> list[Indicator]:
    urlscan_indicators = []
    urlscan_indicators.append(
        Indicator("2-urlscan_globalvariable",
            [
                f"{item['prop']}|{item['type']}" for item in data["data"]["globals"]
            ]
        )
    )
    urlscan_indicators.append(
        Indicator("2-urlscan_cookies",
            [
                f"{item['name']}|{item['domain']}" for item in data["data"]["cookies"]
            ]
        )
    )
    urlscan_indicators.append(
        Indicator("2-urlscan_consolemessages",
            [
                f"{item['message']['level']}|{item['message']['text']}"
                for item in data["data"]["console"]
            ]
        )
    )

    urlscan_indicators.append(
        Indicator("2-urlscan_asn", data["page"]["asn"])
    )
    urlscan_indicators.append(
        Indicator("2-urlscan_domainsonpage", data["lists"]["domains"])
    )
    urlscan_indicators.append(
        Indicator("2-urlscan_urlssonpage", data["lists"]["urls"])
    )

    links = data["data"]["links"]
    hrefs = []
    for link in links:
        hrefs.append(link["href"] + "|" + link["text"])
    urlscan_indicators.append(
        Indicator("2-urlscanhrefs", hrefs)
    )

    # wappalyzer is used to detect tech used in the website
    detected_tech = data["meta"]["processors"]["wappa"]["data"]
    urlscan_indicators.append(
        Indicator("2-techstack", [tech["app"] for tech in detected_tech])
    )
    return urlscan_indicators


# Send a GET request to the specified URL, ignoring bad SSL certificates
def get_endpoints(url, endpoints):
    # for endpoint in endpoints:
    #     response = requests.get(f"{url}/{endpoint}", verify=False)
    #     if response.status_code == 200:
    #         return response.text
    return ''


def parse_cms(url) -> list[Indicator]:
    # TODO: add more CMSs
    cms_indicators = []
    cms = None

    # Endpoints to check for
    wp_endpoints = ["wp-login.php", "wp-admin/"]
    joomla_endpoints = ["administrator/"]
    drupal_endpoints = ["user/login", "core/"]
    bitrix_endpoints = ["bitrix/admin/"]

    # Check for endpoints
    if get_endpoints(url, wp_endpoints) is not None:
        cms_indicators.extend(parse_wordpress(url))
        cms = "WordPress"
    if get_endpoints(url, joomla_endpoints) is not None:
        cms = "Joomla"
    if get_endpoints(url, drupal_endpoints) is not None:
        cms = "Drupal"
    if get_endpoints(url, bitrix_endpoints) is not None:
        cms = "Bitrix"

    if cms is not None:
        cms_indicators.append(Indicator("3-cms", cms))

    return cms_indicators


# For WordPress, check for endpoints, if they exist, get the items and add them as indicators
def parse_wordpress(url) -> list[Indicator]:
    wp_indicators = []
    endpoints = {
        "tags": "/wp-json/wp/v2/tags",
        "posts": "/wp-json/wp/v2/posts",
        "pages": "/wp-json/wp/v2/pages",
        "categories": "/wp-json/wp/v2/categories",
        "users": "/wp-json/wp/v2/users",
        "blocks": "/wp-json/wp/v2/blocks",
    }

    for key, val in endpoints.items():
        try:
            wp_items = json.loads(get_endpoints(url, [val]))
            wp_items_string = ""
            for wp_item in wp_items:
                wp_items_string += wp_item["slug"] + ","
            wp_indicators.append(Indicator("3-wp-" + key, wp_items_string))
        except Exception as e:
            traceback.print_exc()
            continue
    return wp_indicators


def detect_and_parse_feed_content(url) -> list[Indicator]:
    feed_indicators = []
    feed = None

    feed_endpoints = ["rss.xml", "feed/", "rss/"]
    feed = get_endpoints(url, feed_endpoints)

    if feed is not None and feed != "":
        feed = feedparser.parse(url)
        for entry in feed.entries:
            feed_indicators.append(
                Indicator("4-content-title", entry.title)
            )
            feed_indicators.append(Indicator("4-content-link", entry.link))
            feed_indicators.append(
                Indicator("4-content-summary", entry.summary)
            )
            feed_indicators.append(
                Indicator("4-content-published", entry.published)
            )

    return feed_indicators

def get_outbound_domains(url, soup) -> list[Indicator]:
    outbound_domains = set()
    domain_extract = tldextract.extract(url)
    _ = domain_extract.subdomain
    od = domain_extract.domain
    osu = domain_extract.suffix
    a_tags = soup.find_all("a")
    for a_tag in a_tags:
        link_url = a_tag.get('href', '').lower()
        if not link_url or link_url.startswith('tel') or link_url.startswith('mail'):
            continue
        link_extract = tldextract.extract(link_url)
        _ = domain_extract.subdomain
        td = domain_extract.domain
        tsu = domain_extract.suffix
        if tsu and td:
            link_domain = f"{td}.{tsu}"
            if link_domain != f"{od}.{osu}":
                outbound_domains.add(link_domain)
    return [Indicator(content=domain, type="4-outbound-domain") for domain in outbound_domains]


def scrape_url(url):
    # Send a GET request to the specified URL, ignoring bad SSL certificates]
    if len(SCRAPER_API_KEY) > 0:
        try:
            payload = {"api_key": SCRAPER_API_KEY, "url": url}
            return requests.get("https://api.scraperapi.com/", params=payload)
        except requests.exceptions.ConnectionError:
            logging.warn("Unable to use scraper, will use vanilla requests.get")
            traceback.print_exc()
            return requests.get(url, verify=False)
    else:
        return requests.get(url, verify=False)


def crawl(url, run_urlscan=False) -> list[Indicator]:
    indicators = []
    url_submission = None

    if run_urlscan:
        url_submission = start_urlscan(url)

    # Parse the HTML content of the page
    response = scrape_url(url)
    soup = BeautifulSoup(response.text, "html.parser")

    # Run indicators
    indicators.extend(add_response_headers(response=response))
    indicators.extend(add_ip_address(domain_name=url))
    indicators.extend(parse_meta_tags(soup))
    indicators.extend(parse_script_tags(url, soup))
    indicators.extend(parse_iframe_ids(soup))
    indicators.extend(parse_id_attributes(soup))
    indicators.extend(parse_link_tags(url, soup))
    indicators.extend(parse_footer(soup))
    indicators.extend(parse_id_patterns(response=response, id_patterns=EMBEDDED_IDS))
    indicators.extend(parse_id_patterns(response=response, id_patterns=FINANCIAL_IDS))
    indicators.extend(parse_id_patterns(response=response, id_patterns=SOCIAL_MEDIA_IDS))
    indicators.extend(parse_id_patterns(response=response, id_patterns=TRACKING_IDS))
    indicators.extend(add_cdn_domains(soup))
    indicators.extend(parse_domain_name(url))
    indicators.extend(parse_classes(soup))
    indicators.extend(get_ipms_indicators(url))
    indicators.extend(get_shodan_indicators(url))
    indicators.extend(add_associated_domains_from_cert(url))
    indicators.extend(get_outbound_domains(url, soup))
    ## Uncomment the following if needed
    # indicators.extend(add_who_is(url))
    # indicators.extend(parse_images(url, soup, response))
    # indicators.extend(parse_dom_tree(soup))
    # indicators.extend(detect_and_parse_feed_content(url))
    # indicators.extend(parse_cms(url))
    # indicators.extend(parse_sitemaps(url))

    if run_urlscan and url_submission is not None:
        start_time = time.time()  # Record the start time
        while True:
            # Check if 2 minutes have passed
            if time.time() - start_time > 120:
                logging.warn("Timeout: Results not available within 2 minutes.")
                break
            response = requests.get(
                url_submission, headers={"API-Key": URLSCAN_API_KEY}
            )
            if response.status_code == 404:
                logging.warn("Results not ready, retrying in 10 seconds...")
                time.sleep(10)  # Wait for 10 seconds before retrying
            else:
                indicators.extend(add_urlscan_indicators(response.json()))
                break

    return indicators


def crawl_one_or_more_urls(
    urls, run_urlscan=False
) -> list[Indicator]:
    indicators = []
    for url in urls:
        logging.info('Fingerprinting:',url)
        url_indicators = crawl(
            url,
            run_urlscan=run_urlscan,
        )
        domain_name = get_domain_name(url)
        
        for indicator in url_indicators:
            indicator.domain = domain_name
        
        indicators.extend(url_indicators)
    return indicators


def write_domain_indicators(domain, indicators, output_file):
    attribution_table = pd.DataFrame(
        columns=["indicator_type", "indicator_content"],
        data=(indicator.to_dict() for indicator in indicators),
    )
    attribution_table['domain_name'] = domain
    # this is done so if anything bad happens to break the script, we still get partial results
    # this approach also keeps the indicators list from becoming huge and slowing down
    if Path(output_file).exists():
        attribution_table.to_csv(
            output_file,
            index=False,
            mode="a",
            encoding="utf-8",
            header=False,
        )
    else:
        attribution_table.to_csv(
            output_file,
            index=False,
            mode="w",
            encoding="utf-8",
            header=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Match indicators across sites.", add_help=False
    )
    parser.add_argument(
        "-f",
        "--input-file",
        type=str,
        help="file containing list of domains",
        required=False,
        default=os.path.join(".", "sites_of_concern.csv"),
    )
    parser.add_argument(
        "-c", "--domain-column", type=str, required=False, default="Domain"
    )
    # option to run urlscan
    parser.add_argument("-u", "--run-urlscan", type=bool, required=False, default=False)

    parser.add_argument(
        "-o",
        "--output-file",
        type=str,
        help="file to save final list of match results",
        required=False,
        default=os.path.join(".", "indicators_output_dmi.csv"),
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler()
        ]
    )

    args = parser.parse_args()
    domain_col = args.domain_column
    output_file = args.output_file
    run_urlscan = args.run_urlscan
    input_data = pd.read_csv(args.input_file)
    domains = input_data[domain_col]
    for domain in domains:
        try:
            domain_name = get_domain_name(domain)
            indicators = crawl(domain, run_urlscan=run_urlscan)
            write_domain_indicators(domain_name, indicators, output_file=output_file)
        except Exception as e:
            logging.error(f"Failing error on {domain}. See traceback below. Soldiering on...")
            traceback.print_exc()
