import requests
from bs4 import BeautifulSoup
import urllib.parse
import socket
import ipaddress
import logging

logger = logging.getLogger(__name__)

def is_safe_url(url: str) -> bool:
    """
    Checks if the URL resolves to a safe, public IP address.
    Blocks localhost, 127.x.x.x, 10.x.x.x, 172.16.x-172.31.x, 192.168.x.x
    """
    try:
        parsed = urllib.parse.urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return False
            
        # Get IP address for hostname
        ip_str = socket.gethostbyname(hostname)
        ip = ipaddress.ip_address(ip_str)
        
        # Check if IP is global/public
        return ip.is_global
    except Exception as e:
        logger.warning(f"URL validation failed for {url}: {e}")
        return False

def fetch_url_content(url: str) -> dict:
    if not url.startswith("http"):
        url = "http://" + url
        
    if not is_safe_url(url):
        return {"success": False, "error": "This URL resolves to a private or local network address and cannot be fetched for security reasons."}

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        # 15-second timeout
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, "lxml")
        
        # Remove noise
        for tag in soup(["script", "style", "nav", "footer", 
                          "header", "aside", "ads", "iframe"]):
            tag.decompose()
        
        # Extract clean text
        title = soup.title.string.strip() if soup.title else "No title"
        text  = soup.get_text(separator="\n", strip=True)
        
        # Limit to 8000 chars to stay within context window
        text = text[:8000]
        
        return {
            "success": True,
            "url": url,
            "title": title,
            "content": text
        }
    except requests.exceptions.HTTPError as e:
        if e.response.status_code in (401, 403):
            return {"success": False, "error": "This site does not allow reading its content directly. Try copying and pasting the text you want to analyse."}
        return {"success": False, "error": f"HTTP Error: {e.response.status_code}"}
    except requests.exceptions.Timeout:
        return {"success": False, "error": "The page took too long to load."}
    except requests.exceptions.ConnectionError:
        return {"success": False, "error": "Could not connect to that URL."}
    except Exception as e:
        return {"success": False, "error": str(e)}
