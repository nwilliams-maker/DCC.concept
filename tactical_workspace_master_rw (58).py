import streamlit as st
import requests
import base64
import math
import pandas as pd
import time
import hashlib
import json
from datetime import datetime, timedelta
from streamlit_folium import st_folium
import folium
import threading
import os

# --- CONFIG & CREDENTIALS ---
# We check the Environment (Railway) FIRST to avoid the Streamlit Secrets crash
ONFLEET_KEY = os.environ.get("ONFLEET_KEY")
GOOGLE_MAPS_KEY = os.environ.get("GOOGLE_MAPS_KEY")

# If Railway didn't have them, ONLY THEN do we try st.secrets (and we catch the error)
if not ONFLEET_KEY or not GOOGLE_MAPS_KEY:
    try:
        ONFLEET_KEY = ONFLEET_KEY or st.secrets.get("ONFLEET_KEY")
        GOOGLE_MAPS_KEY = GOOGLE_MAPS_KEY or st.secrets.get("GOOGLE_MAPS_KEY")
    except Exception:
        # If we get here, it means no secrets file exists AND Railway variables are missing
        pass

# Final check to keep the app from crashing with a traceback
if not ONFLEET_KEY or not GOOGLE_MAPS_KEY:
    st.error("🔑 **API Keys Missing!**")
    st.info("I couldn't find your keys in Railway's 'Variables' tab. Please double-check that you added ONFLEET_KEY and GOOGLE_MAPS_KEY there.")
    st.stop()

PORTAL_BASE_URL = "https://nwilliams-maker.github.io/Dispatch-Command-Center-rw/portal-dcc-rw.html"
GAS_WEB_APP_URL = "https://script.google.com/macros/s/AKfycbyz16LuLJUJfrtUWxhvK8lGJCVSqRcrqPNOwLEICJ47Oa-BrRnBvFSsy4q8XXo-Y2DTAA/exec"
IC_SHEET_URL = "https://docs.google.com/spreadsheets/d/1y6wX0x93iDc3gdK_nZKLD-2QcGkUHkcM75u90ffRO6k/edit#gid=0"
SAVED_ROUTES_GID = "1477617688"
ACCEPTED_ROUTES_GID = "934075207"
DECLINED_ROUTES_GID = "600909788"
FIELD_NATION_GID = "1396320527"
FINALIZED_ROUTES_GID = "1907347870"

# Terraboost Media Brand Palette
TB_PURPLE = "#633094"
TB_GREEN = "#76bc21"
TB_APP_BG = "#f1f5f9"
TB_HOVER_GRAY = "#e2e8f0"

# Status Fills
TB_GREEN_FILL = "#dcfce7" # Ready
TB_BLUE_FILL = "#dbeafe"  # Sent
TB_RED_FILL = "#fee2e2"   # Flagged
TB_YELLOW_FILL = "#FEF9C3"     # Field Nation
TB_STATIC_FILL = "#f1f5f9"
TB_DIGITAL_FILL = "#ccfbf1"
TB_DIGITAL_BORDER = "#99f6e4" # Teal border

# Standardized Dark Text (for readability)
TB_GREEN_TEXT = "#166534"
TB_RED_TEXT = "#991b1b"
TB_STATIC_TEXT = "#475569"
TB_DIGITAL_TEXT = "#0f766e"

POD_CONFIGS = {
    "Blue": {"states": {"AL", "AR", "FL", "IL", "IA", "LA", "MI", "MN", "MS", "MO", "NC", "SC", "WI"}},
    "Green": {"states": {"CO", "DC", "GA", "IN", "KY", "MD", "NJ", "OH", "UT"}},
    "Orange": {"states": {"AK", "AZ", "CA", "HI", "ID", "NV", "OR", "WA"}},
    "Purple": {"states": {"KS", "MT", "NE", "NM", "ND", "OK", "SD", "TN", "TX", "WY"}},
    "Red": {"states": {"CT", "DE", "ME", "MA", "NH", "NY", "PA", "RI", "VT", "VA", "WV"}}
}

STATE_MAP = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR", "CALIFORNIA": "CA",
    "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE", "FLORIDA": "FL", "GEORGIA": "GA",
    "HAWAII": "HI", "IDAHO": "ID", "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA",
    "KANSAS": "KS", "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD",
    "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN", "MISSISSIPPI": "MS",
    "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE", "NEVADA": "NV", "NEW HAMPSHIRE": "NH",
    "NEW JERSEY": "NJ", "NEW MEXICO": "NM", "NEW YORK": "NY", "NORTH CAROLINA": "NC",
    "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK", "OREGON": "OR", "PENNSYLVANIA": "PA",
    "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD", "TENNESSEE": "TN",
    "TEXAS": "TX", "UTAH": "UT", "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY", "DISTRICT OF COLUMBIA": "DC"
}

headers = {"Authorization": f"Basic {base64.b64encode(f'{ONFLEET_KEY}:'.encode()).decode()}"}

st.set_page_config(page_title="Terraboost Media: Dispatch Command Center", layout="wide")

# --- PINNED TOP-LEFT LOGO ---
# Function to convert the local image into web-safe code
def get_base64_image(image_path):
    try:
        with open(image_path, "rb") as img_file:
            return base64.b64encode(img_file.read()).decode()
    except Exception as e:
        return ""

# Make sure "terraboost_logo.png" perfectly matches your saved file name!
logo_base64 = get_base64_image("terraboost_logo.png")

if logo_base64:
    st.markdown(f"""
        <div style="position: fixed; top: 15px; left: 20px; z-index: 999999;">
            <img src="data:image/png;base64,{logo_base64}" style="width: 140px;"> 
        </div>
    """, unsafe_allow_html=True)
else:
    st.sidebar.error("Logo file not found! Check the file name.")

# --- UI STYLING ---
st.markdown(f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap');
.stApp {{ background-color: {TB_APP_BG} !important; color: #000000 !important; font-family: 'Inter', sans-serif !important; }}
.main .block-container {{ max-width: 1100px !important; padding-top: 2rem; }}

/* =========================================
   WIDGET & INPUT STANDARDIZATION (Fixes the White Box Glitch)
   ========================================= */
/* Force clean white backgrounds on all inputs */
div[data-baseweb="select"] > div,
div[data-baseweb="input"],
div[data-baseweb="input"] > div {{
    background-color: #ffffff !important;
    border-color: #cbd5e1 !important;
}}

/* Ensure text inside inputs is dark and legible */
input[type="text"], 
input[type="number"], 
div[data-baseweb="select"] div {{
    color: #0f172a !important;
    -webkit-text-fill-color: #0f172a !important;
    font-weight: 600 !important;
}}

/* Specifically target and neutralize the Number Input step container */
div[data-testid="stNumberInputContainer"] {{
    border-radius: 8px !important;
    background-color: #ffffff !important;
}}

/* Kills the white box by forcing transparency on the button wrapper */
div[data-testid="stNumberInputContainer"] div[data-baseweb="input"] > div:nth-child(2) {{
    background-color: transparent !important;
}}

/* Style the + / - icons to match the theme */
div[data-testid="stNumberInputContainer"] button, 
div[data-testid="stNumberInputContainer"] svg {{
    color: #64748b !important;
    fill: #64748b !important;
    background-color: transparent !important;
}}

/* GLOBAL TABS CONTAINER - Clean & Floating with Bottom Line */
.stTabs [data-baseweb="tab-list"] {{ 
    justify-content: center; 
    gap: 12px; 
    background: transparent !important; /* Removes the gray box background */
    padding: 15px 15px 20px 15px !important; /* Adds extra padding on the bottom so pills don't touch the line */
    border-bottom: 2px solid #cbd5e1 !important; /* 🌟 THIS IS THE HORIZONTAL LINE 🌟 */
    margin-bottom: 15px !important; /* Pushes the dashboard content down slightly for breathing room */
}}

/* CENTERED PURPLE HEADERS */
h1, h2, h3, h4, h5, h6 {{ 
    font-weight: 800 !important; 
    text-align: center !important; 
    width: 100%;
}}

/* MODERN CONDENSED REFRESH BUTTON - FAR RIGHT */
div.refresh-btn-container {{
    display: flex;
    justify-content: flex-end;
    width: 100%;
}}

div.refresh-btn-container > div > button {{
    height: 32px !important; /* Slightly taller for breathing room */
    padding: 0 16px !important;
    font-size: 13px !important;
    border-radius: 20px !important;
    border: 1.2px solid #633094 !important;
    background-color: transparent !important;
    color: #633094 !important;
    font-weight: 700 !important;
    transition: all 0.2s ease-in-out !important;
    
    /* THE FIX: Forces icon and text onto one line perfectly centered */
    white-space: nowrap !important; 
    display: inline-flex !important;
    align-items: center !important;
    justify-content: center !important;
}}

/* Ensures Streamlit's internal text wrapper doesn't force a line break */
div.refresh-btn-container > div > button div,
div.refresh-btn-container > div > button p {{
    white-space: nowrap !important;
    margin: 0 !important;
    padding: 0 !important;
}}
div.refresh-btn-container > div > button:hover {{
    background-color: #633094 !important;
    color: white !important;
    box-shadow: 0 2px 8px rgba(99, 48, 148, 0.3) !important;
}}

/* GLOBAL TABS STYLING */
.stTabs [data-baseweb="tab-list"] {{ justify-content: center; gap: 8px; background: rgba(255,255,255,0.6); padding: 10px; border-radius: 15px; }}

/* PERMANENT POD TAB OUTLINES & DARK TEXT */
.stTabs [data-baseweb="tab"] {{
    border-top: 1px solid #cbd5e1 !important;
    border-left: 1px solid #cbd5e1 !important;
    border-right: 1px solid #cbd5e1 !important;
    margin: 0 4px !important;
    transition: all 0.2s ease !important;
    font-weight: 800 !important;
    border-radius: 10px 10px 0 0 !important;
    padding: 10px 20px !important;
}}

/* GLOBAL TABS CONTAINER - Clean & Floating */
.stTabs [data-baseweb="tab-list"] {{ 
    justify-content: center; 
    gap: 12px; 
    background: transparent !important; /* Removes the gray box background */
    padding: 15px; 
}}

/* KILL THE DEFAULT UNDERLINE (The "Cutoff" source) */
.stTabs [data-baseweb="tab-highlight"] {{
    background-color: transparent !important;
}}

/* PERMANENT FLOATING PILLS - No flat bottoms */
.stTabs [data-baseweb="tab"] {{
    border-radius: 30px !important; /* Full rounded pill */
    margin: 0 5px !important;
    transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1) !important;
    font-weight: 800 !important;
    padding: 8px 25px !important;
    border: 2px solid transparent !important; /* Invisible border until set below */
}}

/* Global Tab */
.stTabs [data-baseweb="tab"]:nth-of-type(1) {{ border: 2px solid #633094 !important; color: #3b1d58 !important; background: white !important; }}
/* Blue Pod */
.stTabs [data-baseweb="tab"]:nth-of-type(2) {{ border: 2px solid #3b82f6 !important; background-color: #f0f7ff !important; color: #1e3a8a !important; }}
/* Green Pod */
.stTabs [data-baseweb="tab"]:nth-of-type(3) {{ border: 2px solid #22c55e !important; background-color: #f0fdf4 !important; color: #064e3b !important; }}
/* Orange Pod */
.stTabs [data-baseweb="tab"]:nth-of-type(4) {{ border: 2px solid #f97316 !important; background-color: #fffaf5 !important; color: #7c2d12 !important; }}
/* Purple Pod */
.stTabs [data-baseweb="tab"]:nth-of-type(5) {{ border: 2px solid #a855f7 !important; background-color: #faf5ff !important; color: #4c1d95 !important; }}
/* Red Pod */
.stTabs [data-baseweb="tab"]:nth-of-type(6) {{ border: 2px solid #ef4444 !important; background-color: #fef2f2 !important; color: #7f1d1d !important; }}
/* Digital Pool Tab */
.stTabs [data-baseweb="tab"]:nth-of-type(7) {{ border: 2px solid #0f766e !important; color: #0f766e !important; background: #ccfbf1 !important; }}

/* ACTIVE STATE - The "Full Glow" (No flat bottom border) */
.stTabs [aria-selected="true"] {{ 
    background-color: #ffffff !important;
    transform: translateY(-4px) !important; /* Removed the scale(1.05) so it matches cards perfectly */
    box-shadow: 0 10px 20px rgba(99, 48, 148, 0.25) !important; 
}}

/* TAB ACTION BUTTONS (Top Right - Initialize / Sync) */
div.tab-action-btn {{
    display: flex;
    justify-content: flex-end;
    width: 100%;
    margin-top: 0px !important;
}}
div.tab-action-btn > div > button {{
    height: 32px !important;
    padding: 0 24px !important; /* Slightly wider as requested */
    font-size: 13px !important;
    border-radius: 20px !important;
    border: 1.2px solid #633094 !important;
    background-color: transparent !important;
    color: #633094 !important;
    font-weight: 700 !important;
    transition: all 0.2s ease-in-out !important;
    white-space: nowrap !important; 
    display: inline-flex !important;
    align-items: center !important;
    justify-content: center !important;
}}
div.tab-action-btn > div > button:hover {{
    background-color: #633094 !important;
    color: white !important;
    box-shadow: 0 2px 8px rgba(99, 48, 148, 0.3) !important;
}}

/* PRIMARY & SECONDARY BUTTONS */
button[kind="primary"] {{
    background-color: #76bc21 !important;
    color: white !important;
    height: 3.5rem !important;
    font-size: 1.2rem !important;
    font-weight: 800 !important;
    border: none !important;
    box-shadow: 0 4px 6px rgba(0,0,0,0.1) !important;
    transition: all 0.2s ease !important;
}}

button[kind="secondary"] {{
    background-color: #ffffff !important;
    color: {TB_PURPLE} !important;
    border: 2px solid {TB_PURPLE} !important;
    height: 42px !important;
    font-size: 0.9rem !important;
    font-weight: 800 !important;
    border-radius: 8px !important;
    box-shadow: 0 2px 4px rgba(0,0,0,0.05) !important;
    transition: all 0.2s ease !important;
}}

/* =========================================
   1. SCISSORS BUTTON (INSIDE EXPANDER)
   ========================================= */
div[data-testid="stExpander"] div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-child(2) button {{
    margin-top: 2px !important;
    transform: scale(0.7) !important;
    transform-origin: center right !important;
    padding: 0 !important;
    border: none !important;
    box-shadow: none !important;
    background: transparent !important;
}}

/* =========================================
   2. REVOKE POPOVER FUSION (OUTSIDE EXPANDER)
   ========================================= */
/* Target the Expander on the Left side of the gap */
div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"]:nth-child(2) div[data-testid="stPopover"]) > div[data-testid="stColumn"]:nth-child(1) div[data-testid="stExpander"] {{
    border-top-right-radius: 0px !important;
    border-bottom-right-radius: 0px !important;
    border-right: 0px !important;
}}

/* Target the Popover Button on the Right side of the gap */
div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"]:nth-child(1) div[data-testid="stExpander"]) > div[data-testid="stColumn"]:nth-child(2) div[data-testid="stPopover"] > button {{
    margin-left: -1rem !important;
    width: calc(100% + 1rem) !important;
    border-top-left-radius: 0px !important;
    border-bottom-left-radius: 0px !important;
    height: 40px !important; /* 🌟 Matches Expander Height */
    margin-top: 0px !important;
    border: 1px solid #cbd5e1 !important;
    border-left: 1px solid #e2e8f0 !important; /* Subtle inner line */
    background-color: #ffffff !important;
    color: #633094 !important;
    border-radius: 0 10px 10px 0 !important;
    box-shadow: 0 2px 4px rgba(0,0,0,0.02) !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
}}

div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"]:nth-child(1) div[data-testid="stExpander"]) > div[data-testid="stColumn"]:nth-child(2) div[data-testid="stPopover"] > button:hover {{
    background-color: #fcfaff !important;
    border-color: #cbd5e1 !important;
}}

/* Main Expander Container */
div[data-testid="stExpander"] {{ 
    border: 1px solid #cbd5e1 !important; 
    border-radius: 10px !important; 
    box-shadow: 0 2px 4px rgba(0,0,0,0.02) !important;
    margin-bottom: 8px !important;
    background-color: #ffffff !important;
    overflow: hidden !important;
}}

/* Header text color */
div[data-testid="stExpander"] details summary p {{ 
    color: #000000 !important; 
    font-weight: 800 !important; 
    font-size: 0.85rem !important;
}}

/* 🚀 FIX: STOP THE DARK HOVER & BLACK CLICK FILL */
div[data-testid="stExpander"] details summary {{
    background-color: #ffffff !important; /* Force base color */
    transition: background-color 0.2s ease !important;
}}

div[data-testid="stExpander"] details summary:hover {{
    background-color: #fcfaff !important; /* Very light purple on hover */
}}

/* This targets the exact moment you click it */
div[data-testid="stExpander"] details summary:active {{
    background-color: #ffffff !important; 
}}

/* This removes the "Black/Gray Box" focus state that stays after clicking */
div[data-testid="stExpander"] details summary:focus, 
div[data-testid="stExpander"] details summary:focus-visible {{
    background-color: #ffffff !important;
    outline: none !important;
    box-shadow: none !important;
}}

/* Ensure the text stays visible during the click */
div[data-testid="stExpander"] details summary:hover p,
div[data-testid="stExpander"] details summary:active p,
div[data-testid="stExpander"] details summary:focus p {{
    color: #633094 !important;
}}

label, div[data-testid="stWidgetLabel"] p {{ color: #000000 !important; font-weight: 600 !important; }}

/* MAP & FOLIUM */
iframe[title="streamlit_folium.st_folium"] {{
    border-radius: 15px !important;
    box-shadow: 0 4px 6px rgba(0,0,0,0.05) !important;
}}
.stFolium {{ background: transparent !important; }}

/* =========================================
   UNIFIED HOVER & CLICK EFFECTS
   ========================================= */

/* 1. BUTTONS: Lift + Purple Glow */
button[kind="primary"]:hover,
button[kind="secondary"]:hover,
div.refresh-btn-container > div > button:hover {{
    transform: translateY(-4px) !important;
    box-shadow: 0 12px 28px rgba(99, 48, 148, 0.35) !important;
    border-color: #633094 !important;
    z-index: 10;
}}

/* 2. CARDS, TABS & EXPANDERS: Lift + Neutral Drop Shadow (No Purple) */
div[data-testid="stExpander"]:hover,
.pod-card-pill:hover,
.dashboard-supercard:hover,
.stTabs [data-baseweb="tab"]:hover {{
    transform: translateY(-4px) !important;
    box-shadow: 0 12px 24px rgba(0, 0, 0, 0.08) !important; 
    z-index: 10;
}}

/* 3. STRICT CLICK ANIMATION (Kills the "Push In" effect) */
/* Forces all elements to just drop back to baseline smoothly when clicked */
button[kind="primary"]:active,
button[kind="secondary"]:active,
div.refresh-btn-container > div > button:active,
div[data-testid="stExpander"] details summary:active,
.pod-card-pill:active,
.dashboard-supercard:active,
.stTabs [data-baseweb="tab"]:active {{
    transform: translateY(0px) scale(1) !important; 
    box-shadow: 0 2px 4px rgba(0,0,0,0.05) !important;
}}

/* Smooth transitions for everything */
div[data-testid="stExpander"],
div[data-testid="stExpander"] details summary,
.pod-card-pill,
.dashboard-supercard,
button[kind="primary"],
button[kind="secondary"],
div.refresh-btn-container > div > button,
.stTabs [data-baseweb="tab"] {{
    transition: all 0.3s cubic-bezier(0.25, 0.8, 0.25, 1) !important;
}}

/* =========================================
   SUB-TAB PILL STYLING (Column-Targeting Method)
   ========================================= */

/* --- LEFT COLUMN: Dispatch Tabs --- */
/* 1. Ready (Green) */
div[data-testid="stColumn"]:nth-child(1) div[data-testid="stTabs"] [data-baseweb="tab"]:nth-of-type(1) {{
    background-color: #dcfce7 !important;
    border: 2px solid #166534 !important;
}}
div[data-testid="stColumn"]:nth-child(1) div[data-testid="stTabs"] [data-baseweb="tab"]:nth-of-type(1) p {{
    color: #166534 !important; 
}}

/* 2. Flagged (Red) */
div[data-testid="stColumn"]:nth-child(1) div[data-testid="stTabs"] [data-baseweb="tab"]:nth-of-type(2) {{
    background-color: #fee2e2 !important;
    border: 2px solid #991b1b !important;
}}
div[data-testid="stColumn"]:nth-child(1) div[data-testid="stTabs"] [data-baseweb="tab"]:nth-of-type(2) p {{
    color: #991b1b !important; 
}}

/* 3. Field Nation (Light Yellow BG / Dark Yellow Text) */
div[data-testid="stColumn"]:nth-child(1) div[data-testid="stTabs"] [data-baseweb="tab"]:nth-of-type(3) {{
    background-color: #fef9c3 !important;
    border: 2px solid #854d0e !important;
    border-radius: 30px !important;
    margin: 0 5px !important;
}}
div[data-testid="stColumn"]:nth-child(1) div[data-testid="stTabs"] [data-baseweb="tab"]:nth-of-type(3) p {{
    color: #854d0e !important;
    font-weight: 800 !important;
}}

/* 4. Digital (Teal - Left Column) */
div[data-testid="stColumn"]:nth-child(1) div[data-testid="stTabs"] [data-baseweb="tab"]:nth-of-type(4) {{
    background-color: #ccfbf1 !important;
    border: 2px solid #0f766e !important;
    border-radius: 30px !important;
    margin: 0 5px !important;
}}
div[data-testid="stColumn"]:nth-child(1) div[data-testid="stTabs"] [data-baseweb="tab"]:nth-of-type(4) p {{
    color: #0f766e !important;
    font-weight: 800 !important;
}}

/* --- RIGHT COLUMN: Awaiting Tabs --- */
/* Force the gap, center the pills, and stop stretching */
div[data-testid="stColumn"]:nth-child(2) div[data-testid="stTabs"] [data-baseweb="tab-list"] {{
    gap: 12px !important;
    justify-content: center !important; 
}}

/* 🌟 RESTORE PILL SIZE: Inherit global sizing but prevent stretching */
div[data-testid="stColumn"]:nth-child(2) div[data-testid="stTabs"] [data-baseweb="tab"] {{
    flex-grow: 0 !important; /* Kills the stretching bloat */
}}

div[data-testid="stColumn"]:nth-child(2) div[data-testid="stTabs"] [data-baseweb="tab"] p {{
    white-space: nowrap !important;
    font-weight: 800 !important; /* Matches left column boldness */
}}

/* 1. Sent (Purple/Blue) */
div[data-testid="stColumn"]:nth-child(2) div[data-testid="stTabs"] [data-baseweb="tab"]:nth-of-type(1) {{
    background-color: #f3e8ff !important;
    border: 2px solid #633094 !important;
    border-radius: 30px !important;
}}
div[data-testid="stColumn"]:nth-child(2) div[data-testid="stTabs"] [data-baseweb="tab"]:nth-of-type(1) p {{
    color: #633094 !important; 
}}

/* 2. Accepted (Green) */
div[data-testid="stColumn"]:nth-child(2) div[data-testid="stTabs"] [data-baseweb="tab"]:nth-of-type(2) {{
    background-color: #dcfce7 !important;
    border: 2px solid #166534 !important;
    border-radius: 30px !important;
}}
div[data-testid="stColumn"]:nth-child(2) div[data-testid="stTabs"] [data-baseweb="tab"]:nth-of-type(2) p {{
    color: #166534 !important; 
}}

/* 3. Declined (Red) */
div[data-testid="stColumn"]:nth-child(2) div[data-testid="stTabs"] [data-baseweb="tab"]:nth-of-type(3) {{
    background-color: #fee2e2 !important;
    border: 2px solid #991b1b !important;
    border-radius: 30px !important;
}}
div[data-testid="stColumn"]:nth-child(2) div[data-testid="stTabs"] [data-baseweb="tab"]:nth-of-type(3) p {{
    color: #991b1b !important; 
}}

/* 4. Finalized (Orange) */
div[data-testid="stColumn"]:nth-child(2) div[data-testid="stTabs"] [data-baseweb="tab"]:nth-of-type(4) {{
    background-color: #fffaf5 !important;
    border: 2px solid #f97316 !important;
    border-radius: 30px !important;
}}
div[data-testid="stColumn"]:nth-child(2) div[data-testid="stTabs"] [data-baseweb="tab"]:nth-of-type(4) p {{
    color: #7c2d12 !important; 
}}

/* ALIGN COLUMNS AT THE TOP (Fixes the giant gap on the left) */
div[data-testid="stHorizontalBlock"] {{ align-items: flex-start !important; }}

/* TIGHTEN GAPS BETWEEN CARDS */
div[data-testid="stVerticalBlock"] {{ gap: 1rem !important; }}
div[data-testid="stExpander"] {{ margin-top: 0px !important; margin-bottom: 2px !important; }}

/* MINI REVOKE BUTTON (Single Line, Right Aligned) */
div.mini-btn button {{
    height: 30px !important;
    min-height: 30px !important;
    padding: 0 8px !important;
    font-size: 11px !important;
    white-space: nowrap !important; /* CRITICAL: Stops "Revoke" from dropping to a second line */
    float: right !important;
    margin-top: 4px !important;
    border-radius: 4px !important;
}}

</style>
""", unsafe_allow_html=True)

# --- 1. BACKGROUND THREAD WORKER ---
def background_sheet_move(cluster_hash, payload_json):
    """Silent worker to update Google Sheets without freezing the UI."""
    try:
        # Sends data silently in the background
        requests.post(GAS_WEB_APP_URL, json={
            "action": "archiveRoute", 
            "cluster_hash": cluster_hash,
            "payload": payload_json if payload_json else {} 
        }, timeout=15)
    except:
        pass 
        
# --- 2. INSTANT REVOKE LOGIC ---
def move_to_dispatch(cluster_hash, ic_name, pod_name, action_label="Revoked", check_onfleet=True, cluster_data=None):
    """Moves route to Dispatch column instantly and schedules a background Onfleet scrub."""
    
    # 1. 🚀 FIRE AND FORGET: 
    # Launch the slow Google Sheets update in a separate lane so the app doesn't freeze
    threading.Thread(target=background_sheet_move, args=(cluster_hash, cluster_data), daemon=True).start()
        
    # 2. 🛡️ THE FIX: Set the reverted flag to True
    # This forces the UI logic to ignore the Google Sheet record for 15 seconds
    st.session_state[f"reverted_{cluster_hash}"] = True
    
    # 3. 🧠 INSTANT RESET: Clear column-shifting flags
    st.session_state.pop(f"route_state_{cluster_hash}", None)
    st.session_state.pop(f"sent_ts_{cluster_hash}", None)
    st.session_state.pop(f"contractor_{cluster_hash}", None)
    st.session_state.pop(f"sync_{cluster_hash}", None)
    
    # 4. 🛡️ SCHEDULE SCRUB: 5-second deferred check for Onfleet
    st.session_state[f"scrub_timer_{cluster_hash}"] = time.time() + 5
    
    st.toast(f"✅ {action_label}! Route moved back to Dispatch.")
    # 🌟 THE FIX: st.rerun() has been completely removed!
    
def background_sheet_finalize(cluster_hash):
    """Silent worker to finalize routes in Google Sheets without freezing the UI."""
    try:
        requests.post(GAS_WEB_APP_URL, json={"action": "finalizeRoute", "cluster_hash": cluster_hash}, timeout=15)
    except:
        pass

@st.fragment
def render_finalization_checklist(cluster_hash, pod_name, prefix="chk"):
    """Isolates checkbox reruns so the whole page doesn't reload, making checks instant."""
    st.markdown("<p style='font-size: 13px; font-weight: 600;'>Finalization Checklist:</p>", unsafe_allow_html=True)
    cc1, cc2, cc3 = st.columns(3)
    chk1 = cc1.checkbox("Optimized Route in OnFleet.", key=f"{prefix}1_{cluster_hash}_{pod_name}")
    chk2 = cc2.checkbox("Dispatched in Route Planning.", key=f"{prefix}2_{cluster_hash}_{pod_name}")
    chk3 = cc3.checkbox("Packing list created.", key=f"{prefix}3_{cluster_hash}_{pod_name}")
    
    if chk1 and chk2 and chk3:
        # 🌟 THE FIX: Handled procedurally instead of via callback to escape the fragment trap!
        if st.button("🏁 Finalize Route", key=f"finbtn_{prefix}_{cluster_hash}_{pod_name}", type="primary", use_container_width=True):
            # 1. 🚀 FIRE AND FORGET
            threading.Thread(target=background_sheet_finalize, args=(cluster_hash,), daemon=True).start()
            
            # 2. 🧠 INSTANT UI OVERRIDE
            st.session_state[f"route_state_{cluster_hash}"] = "finalized"
            st.session_state[f"reverted_{cluster_hash}"] = True 
            
            st.toast("🏁 Route Finalized! Moving to Finalized tab...")
            time.sleep(0.1) # Tiny pause to guarantee the toast visually registers
            st.rerun(scope="app") # 🌟 THE FIX: Blasts through the fragment to refresh the whole dashboard!
        

    
def instant_revoke_handler(cluster_hash, ic_name, payload_json, pod_name):
    # We now enable Onfleet scrubbing (State 0 check) immediately
    move_to_dispatch(cluster_hash, ic_name, pod_name, action_label="Revoked", check_onfleet=True, cluster_data=payload_json)
    
# --- UTILITIES ---
def haversine(lat1, lon1, lat2, lon2):
    R = 3958.8
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def normalize_state(st_str):
    if not st_str: return "UNKNOWN"
    clean = str(st_str).strip().upper()
    return STATE_MAP.get(clean, clean)

@st.cache_data(ttl=15, show_spinner=False)
def fetch_sent_records_from_sheet():
    try:
        # Use safe extraction to prevent KeyError/AttributeError
        if not isinstance(IC_SHEET_URL, str):
            raise ValueError("IC_SHEET_URL must be a string. Check for trailing commas!")
            
        base_url = f"{IC_SHEET_URL.split('/edit')[0]}/export?format=csv&gid="
        
        sheets_to_fetch = [
            (DECLINED_ROUTES_GID, "declined"),
            (ACCEPTED_ROUTES_GID, "accepted"),
            (SAVED_ROUTES_GID, "sent"),
        ]

       # 3. Add Field Nation only if the GID is defined to avoid errors
        if 'FIELD_NATION_GID' in globals() and FIELD_NATION_GID:
            # We check if it's already there to prevent duplicates
            if (FIELD_NATION_GID, "field_nation") not in sheets_to_fetch:
                sheets_to_fetch.append((FIELD_NATION_GID, "field_nation"))
                
        # 🌟 NEW: Add Finalized routes to the download queue
        if 'FINALIZED_ROUTES_GID' in globals() and FINALIZED_ROUTES_GID:
            if (FINALIZED_ROUTES_GID, "finalized") not in sheets_to_fetch:
                sheets_to_fetch.append((FINALIZED_ROUTES_GID, "finalized"))
        
        sent_dict = {}
        # 🌟 THE FIX: Add Global_Digital to the dictionary!
        ghost_routes = {"Blue": [], "Green": [], "Orange": [], "Purple": [], "Red": [], "Global_Digital": [], "UNKNOWN": []}
        
        for gid, status_label in sheets_to_fetch:
            try:
                # Ensure gid is cast to string just in case it's an integer
                df = pd.read_csv(base_url + str(gid))
                df.columns = [str(c).strip().lower() for c in df.columns]
                
                if 'json payload' in df.columns:
                    for _, row in df.iterrows():
                        try:
                            p = json.loads(row['json payload'])
                            tids = str(p.get('taskIds', '')).replace('|', ',').split(',')
                            c_name = str(row.get('contractor', 'Unknown Contractor'))
                            
                            raw_ts = row.get('date created', '')
                            ts_display = ""
                            if pd.notna(raw_ts) and str(raw_ts).strip():
                                try:
                                    ts_display = pd.to_datetime(raw_ts).strftime('%m/%d %I:%M %p')
                                except:
                                    ts_display = str(raw_ts)
                            
                            # 1. Live Task Matching
                            for tid in tids:
                                tid = tid.strip()
                                if tid:
                                    # 1. Enforce Contractor name for FN routes
                                    display_name = "Field Nation" if status_label == "field_nation" else c_name
                                    sent_dict[tid] = {
                                        "name": display_name, 
                                        "status": status_label,
                                        "time": ts_display,
                                        "wo": p.get('wo', display_name),
                                        "comp": p.get('comp', 0),     
                                        "due": p.get('due', 'N/A')    
                                    }
                            
                            # 🌟 THE FIX: Omni-Ghost Engine - Capture Sent routes too!
                            if status_label in ['accepted', 'finalized', 'sent']:
                                locs_str = str(p.get('locs', ''))
                                state_guess, city_guess = "UNKNOWN", "Unknown"
                                stops_list = [s.strip() for s in locs_str.split('|') if s.strip()]
                                
                                # Extract city/state from the first real stop
                                if len(stops_list) > 1:
                                    addr_parts = stops_list[1].split(',')
                                    if len(addr_parts) >= 2:
                                        state_raw = addr_parts[-1].strip().upper()
                                        state_guess = state_raw.split(' ')[0] # 🌟 THE FIX: Strips out zip codes!
                                        city_guess = addr_parts[-2].strip()
                                
                                # 🌟 THE FIX: Bulletproof Digital Ghost Detection
                                # Check the raw JSON payload first, then fallback to emojis
                                is_digital_ghost = False
                                
                                # Try extracting from raw taskIds if we have them
                                if tids and tids[0].strip() in sent_dict:
                                    is_digital_ghost = sent_dict[tids[0].strip()].get('is_digital', False)
                                    
                                # If it's not explicitly labeled in the dictionary, scan the job string
                                if not is_digital_ghost:
                                    job_only = str(p.get('jobOnly', ''))
                                    # Added "Service", "Offline", "Ins/Rem" text matches just in case emojis were stripped
                                    is_digital_ghost = any(trigger in job_only.lower() for trigger in ['🔌', '🔧', '⚙️', '📵', 'service', 'offline', 'ins/rem'])
                                
                                pod_name = "UNKNOWN"
                                if is_digital_ghost:
                                    pod_name = "Global_Digital"
                                else:
                                    norm_state = STATE_MAP.get(state_guess, state_guess)
                                    for p_name, p_config in POD_CONFIGS.items():
                                        if norm_state in p_config['states']:
                                            pod_name = p_name
                                            break
                                
                                if pod_name != "UNKNOWN":
                                    clean_tids = [str(t).strip() for t in tids if str(t).strip()]
                                    ghost_hash = hashlib.md5("".join(sorted(clean_tids)).encode()).hexdigest()

                                    ghost_routes[pod_name].append({
                                        "contractor_name": c_name,
                                        "route_ts": ts_display,
                                        "city": city_guess,
                                        "state": norm_state,
                                        "stops": p.get('lCnt', 0),
                                        "tasks": p.get('tCnt', len(tids)),
                                        "pay": p.get('comp', 0),
                                        "wo": p.get('wo', c_name),
                                        "due": p.get('due', 'N/A'),  
                                        "status": status_label, # 🌟 NEW: Save the DB status so the Traffic Cop knows!
                                        "hash": ghost_hash, 
                                        "locs": p.get('locs', '') 
                                    })
                        except Exception: continue
            except Exception: continue
            
        return sent_dict, ghost_routes
    except Exception as e:
        st.error(f"Failed to fetch portal records: {e}")
        return {}, {}

# 🌟 ADDED ttl=3600 so the cache clears every hour to grab fresh traffic data
@st.cache_data(ttl=3600, show_spinner=False)
def get_gmaps(home, waypoints):
    # 🌟 ADDED departure_time=now to force Google to calculate Live Traffic
    url = f"https://maps.googleapis.com/maps/api/directions/json?origin={home}&destination={home}&waypoints=optimize:true|{'|'.join(waypoints)}&departure_time=now&key={GOOGLE_MAPS_KEY}"
    try:
        res = requests.get(url).json()
        if res['status'] == 'OK':
            mi = sum(l['distance']['value'] for l in res['routes'][0]['legs']) * 0.000621371
            
            # This is the raw driving time with live traffic
            drive_hrs = sum(l['duration']['value'] for l in res['routes'][0]['legs']) / 3600
            
            # 🌟 NEW: Add "Service Time" (e.g., 15 minutes / 0.25 hours per stop)
            # You can change 0.25 to whatever average time you expect them to be at a location
            service_hrs = len(waypoints) * 0.25 
            
            total_hrs = drive_hrs + service_hrs
            
            return round(mi, 1), total_hrs, f"{int(total_hrs)}h {int((total_hrs * 60) % 60)}m"
    except: pass
    return 0, 0, "0h 0m"

@st.cache_data(ttl=600)
def load_ic_database(sheet_url):
    try:
        export_url = f"{sheet_url.split('/edit')[0]}/export?format=csv&gid=0"
        return pd.read_csv(export_url)
    except: return None

def process_digital_pool(master_bar=None):
    prog_bar = master_bar if master_bar else st.progress(0)
    prog_bar.progress(0.1, text="📥 Fetching National Tasks from Onfleet...")
    
    # 1. Fetch Onfleet (ONCE)
    APPROVED_TEAMS = ["a - escalation", "b - boosted campaigns", "b - local campaigns", "c - priority nationals", "cvs kiosk removal", "cvs kiosk removals", "d - digital routes", "n - national campaigns"]
    teams_res = requests.get("https://onfleet.com/api/v2/teams", headers=headers).json()
    target_team_ids = [t['id'] for t in teams_res if any(appr in str(t.get('name', '')).lower() for appr in APPROVED_TEAMS)]
    esc_team_ids = [t['id'] for t in teams_res if 'escalation' in str(t.get('name', '')).lower()]

    all_tasks_raw = []
    time_window = int(time.time()*1000) - (45*24*3600*1000)
    url = f"https://onfleet.com/api/v2/tasks/all?state=0&from={time_window}"
    
    while url:
        response = requests.get(url, headers=headers)
        if response.status_code == 429:
            time.sleep(2); continue
        if response.status_code != 200: break
        res_json = response.json()
        all_tasks_raw.extend(res_json.get('tasks', []))
        url = f"https://onfleet.com/api/v2/tasks/all?state=0&from={time_window}&lastId={res_json['lastId']}" if res_json.get('lastId') else None
        
    prog_bar.progress(0.4, text="🔍 Isolating Digital Service Calls...")
    
    # 🌟 STRICT DIGITAL FILTER
    # --- 🌟 STRICT DIGITAL FILTER ---
    DIGITAL_WHITELIST = ["service", "ins/rem", "offline"]
    fresh_sent_db, _ = fetch_sent_records_from_sheet()
    st.session_state.sent_db = fresh_sent_db

    pool = []
    unique_tasks_dict = {t['id']: t for t in all_tasks_raw}
    
    for t in unique_tasks_dict.values():
        container = t.get('container', {})
        c_type = str(container.get('type', '')).upper()
        if c_type == 'TEAM' and container.get('team') not in target_team_ids: continue

        addr = t.get('destination', {}).get('address', {})
        stt = normalize_state(addr.get('state', ''))
        is_esc = (c_type == 'TEAM' and container.get('team') in esc_team_ids)
        
        # --- 🔍 STRICT CLASSIFICATION ENGINE (v4 - Final) ---
        native_details = str(t.get('taskDetails', '')).strip()
        custom_fields = t.get('customFields') or []
        
        # 1. EXTRACT OFFICIAL CUSTOM FIELDS
        custom_task_type = ""
        custom_boosted = ""
        
        # Default UI display to native details unless a custom field overwrites it
        tt_val = native_details 
        
        for f in custom_fields:
            f_name = str(f.get('name', '')).strip().lower()
            f_key = str(f.get('key', '')).strip().lower()
            f_val = str(f.get('value', '')).strip()
            f_val_lower = f_val.lower()
            
            # Capture Official 'Task Type' Custom Field
            if f_name in ['task type', 'tasktype'] or f_key in ['tasktype', 'task_type']:
                custom_task_type = f_val_lower
                tt_val = f_val # 🌟 UI Display is now officially the Custom Field
                
            # Capture 'Boosted Standard' Custom Field
            if f_name in ['boosted standard', 'boostedstandard'] or f_key in ['boostedstandard', 'boosted_standard']:
                custom_boosted = f_val_lower
                
            # Capture Escalation
            if 'escalation' in f_name or 'escalation' in f_key:
                if f_val_lower in ['1', '1.0', 'true', 'yes'] or 'escalation' in f_val_lower:
                    is_esc = True

        # 2. CHECK REGULAR (STATIC) EXEMPTIONS FIRST
        # Expanded to include "escalation" to prevent crossing over
        search_string = f"{native_details} {custom_task_type}".lower()
        REGULAR_EXEMPTIONS = ["photo", "magnet", "continuity", "new ad", "pull down", "kiosk install", "kiosk removal", "escalation"]
        is_exempt = any(ex in search_string for ex in REGULAR_EXEMPTIONS)
        
        # 3. STRICT DIGITAL CHECK
        is_digital_task = False

        if not is_exempt:
            # Rule A: Task Type contains service, ins/rem, or offline
            if any(trigger in custom_task_type for trigger in ["service", "ins/rem", "offline"]):
                is_digital_task = True
            # 🌟 Rule B: Boosted Standard contains the word 'digital' (Matches 'Premium_Digital')
            elif "digital" in custom_boosted:
                is_digital_task = True
        
        # 🌟 SPEED FIX: Skip routing math entirely if it's not strictly digital
        if not is_digital_task: 
            continue
            
        # --- 4. ASSIGN STATUS & POOL ---
        t_status = fresh_sent_db.get(t['id'], {}).get('status', 'ready').lower() if t['id'] in fresh_sent_db else 'ready'
        t_wo = fresh_sent_db.get(t['id'], {}).get('wo', 'none') if t['id'] in fresh_sent_db else 'none'
        
        pool.append({
            "id": t['id'], "city": addr.get('city', 'Unknown'), "state": stt,
            "full": f"{addr.get('number','')} {addr.get('street','')}, {addr.get('city','')}, {stt}",
            "lat": t['destination']['location'][1], "lon": t['destination']['location'][0],
            "escalated": is_esc, "task_type": tt_val, "is_digital": True, "db_status": t_status, "wo": t_wo
        })

    prog_bar.progress(0.6, text=f"🗺️ Routing {len(pool)} Digital Tasks...")
    
    # 3. Route ONLY the Digital Tasks
    ic_df = st.session_state.get('ic_df', pd.DataFrame())
    lat_col = next((col for col in ic_df.columns if 'lat' in str(col).lower()), 'lat')
    lng_col = next((col for col in ic_df.columns if 'lng' in str(col).lower()), 'lng')
    v_ics_base = ic_df[~ic_df.astype(str).apply(lambda x: x.str.contains('Field Agent', case=False, na=False).any(), axis=1)].dropna(subset=[lat_col, lng_col]).copy() if (lat_col in ic_df.columns and lng_col in ic_df.columns) else pd.DataFrame()

    clusters = []
    route_radius = 25 # Strict 25-mile radius for digital
    
    while pool:
        anc = pool.pop(0)
        candidates = []
        rem = []
        
        for t in pool:
            if anc['db_status'] in ['sent', 'accepted', 'field_nation']:
                if t['db_status'] == anc['db_status'] and t['wo'] == anc['wo']: candidates.append((0, t))
                else: rem.append(t)
            elif anc['db_status'] in ['ready', 'declined']:
                if t['db_status'] in ['ready', 'declined']:
                    d = haversine(anc['lat'], anc['lon'], t['lat'], t['lon'])
                    if d <= route_radius: candidates.append((d, t))
                    else: rem.append(t)
                else: rem.append(t)
        
        candidates.sort(key=lambda x: x[0])
        
        group = [anc]
        unique_stops = {anc['full']}
        spillover = []
        for _, t in candidates:
            if len(unique_stops) < 20 or t['full'] in unique_stops:
                group.append(t); unique_stops.add(t['full'])
            else: spillover.append(t)
        rem.extend(spillover)
        
        has_ic = False
        ic_dist = 0
        if not v_ics_base.empty:
            dists = [haversine(anc['lat'], anc['lon'], lat, lng) for lat, lng in zip(v_ics_base[lat_col], v_ics_base[lng_col])]
            valid_ics = v_ics_base.copy()
            valid_ics['d'] = dists
            valid_ics = valid_ics[valid_ics['d'] <= 100]
            if not valid_ics.empty:
                best_ic = valid_ics.sort_values('d').iloc[0]
                has_ic = True
                ic_dist = best_ic['d']

        status = "Ready" if anc['db_status'] not in ['sent', 'accepted', 'finalized'] else anc['db_status'].capitalize()
        if status == "Ready" and (not has_ic or ic_dist > 60): status = "Flagged"

        clusters.append({
            "data": group, "center": [anc['lat'], anc['lon']], "stops": len(unique_stops), 
            "city": anc['city'], "state": anc['state'], "status": status, "has_ic": has_ic,
            "esc_count": sum(1 for x in group if x.get('escalated')),
            "is_digital": True,
            "inst_count": sum(1 for x in group if "install" in str(x.get('task_type', '')).lower()),
            "remov_count": sum(1 for x in group if "remove" in str(x.get('task_type', '')).lower()),
            "wo": anc['wo']
        })
        pool = rem

    # Save to dedicated Global Digital State
    st.session_state['global_digital_clusters'] = clusters
    prog_bar.empty()

# --- CORE LOGIC ---
def process_pod(pod_name, master_bar=None, pod_idx=0, total_pods=1):
    config = POD_CONFIGS[pod_name]
    
    # Logic to handle if we are doing a single pod or a global pull
    pod_weight = 1.0 / total_pods
    start_pct = pod_idx * pod_weight
    
    # Use the master bar if provided, otherwise create a local one
    prog_bar = master_bar if master_bar else st.progress(0)
    
    def update_prog(rel_val, msg):
        # Maps a 0.0-1.0 internal progress to the global start/end points
        global_val = min(start_pct + (rel_val * pod_weight), 0.99)
        prog_bar.progress(global_val, text=f"[{pod_name}] {msg}")

    try:
        update_prog(0.0, "📥 Extracting tasks...")
        APPROVED_TEAMS = [
            "a - escalation", "b - boosted campaigns", "b - local campaigns", 
            "c - priority nationals", "cvs kiosk removal", "digital routes", "n - national campaigns"
        ]

        teams_res = requests.get("https://onfleet.com/api/v2/teams", headers=headers).json()
        target_team_ids = [t['id'] for t in teams_res if any(appr in str(t.get('name', '')).lower() for appr in APPROVED_TEAMS)]
        esc_team_ids = [t['id'] for t in teams_res if 'escalation' in str(t.get('name', '')).lower()]

        all_tasks_raw = []
        # Change the 80 to 45 right here:
        url = f"https://onfleet.com/api/v2/tasks/all?state=0&from={int(time.time()*1000)-(45*24*3600*1000)}"
        
        all_tasks_raw = []
        # 🌟 FIX 1: Change 80 to 45 days
        time_window = int(time.time()*1000) - (45*24*3600*1000)
        url = f"https://onfleet.com/api/v2/tasks/all?state=0&from={time_window}"
        
        while url:
            response = requests.get(url, headers=headers)
            
            # Handle Rate Limiting (Error 429) dynamically
            if response.status_code == 429:
                st.toast("⚠️ Onfleet Throttling... waiting 2 seconds.")
                time.sleep(2)
                continue
            
            if response.status_code != 200:
                st.error(f"Onfleet API Error: {response.json()}")
                break
                
            res_json = response.json()
            tasks_page = res_json.get('tasks', [])
            all_tasks_raw.extend(tasks_page)
            
            # 🚀 OPTIMIZATION: Removed the time.sleep(0.5) here!
            
            url = f"https://onfleet.com/api/v2/tasks/all?state=0&from={time_window}&lastId={res_json['lastId']}" if res_json.get('lastId') else None
            update_prog(min(len(all_tasks_raw)/500 * 0.4, 0.4), "📡 Fetching tasks...")

        unique_tasks_dict = {t['id']: t for t in all_tasks_raw}
        all_tasks = list(unique_tasks_dict.values())
        
        # --- 🌟 1. DEFINE SPECIFIC DIGITAL TRIGGERS ---
        # We search for these exact keywords within your data
        DIGITAL_WHITELIST = ["ins/remove", "offline", "service"]

        # PERFORMANCE FIX: Fetch Google Sheets data once before the loop
        fresh_sent_db, _ = fetch_sent_records_from_sheet()
        st.session_state.sent_db = fresh_sent_db

        pool = []
        for t in all_tasks:
            container = t.get('container', {})
            c_type = str(container.get('type', '')).upper()
            
            if c_type == 'TEAM' and container.get('team') not in target_team_ids: 
                continue

            addr = t.get('destination', {}).get('address', {})
            stt = normalize_state(addr.get('state', ''))
            is_esc = (c_type == 'TEAM' and container.get('team') in esc_team_ids)
            
            # --- 🔍 STRICT CLASSIFICATION ENGINE (v5) ---
            native_details = str(t.get('taskDetails', '')).strip()
            custom_fields = t.get('customFields') or []
            
            # 1. EXTRACT OFFICIAL CUSTOM FIELDS
            custom_task_type = ""
            custom_boosted = ""
            tt_val = native_details # Fallback UI display
            
            for f in custom_fields:
                f_name = str(f.get('name', '')).strip().lower()
                f_key = str(f.get('key', '')).strip().lower()
                f_val = str(f.get('value', '')).strip()
                f_val_lower = f_val.lower()
                
                # Capture Official 'Task Type'
                if f_name in ['task type', 'tasktype'] or f_key in ['tasktype', 'task_type']:
                    custom_task_type = f_val_lower
                    tt_val = f_val # Set the UI badge text
                    
                # Capture Official 'Boosted Standard'
                if f_name in ['boosted standard', 'boostedstandard'] or f_key in ['boostedstandard', 'boosted_standard']:
                    custom_boosted = f_val_lower
                    
                # Capture Escalation (Adds the ⭐)
                if 'escalation' in f_name or 'escalation' in f_key:
                    if f_val_lower in ['1', '1.0', 'true', 'yes'] or 'escalation' in f_val_lower:
                        is_esc = True

            # 2. CHECK REGULAR (STATIC) EXEMPTIONS FIRST
            # Combines native and custom type to ensure "Magnet" or "Photo" are never missed
            search_string = f"{native_details} {custom_task_type}".lower()
            REGULAR_EXEMPTIONS = ["photo", "magnet", "continuity", "new ad", "pull down", "kiosk", "escalation"]
            is_exempt = any(ex in search_string for ex in REGULAR_EXEMPTIONS)
            
            # 3. APPLY DIGITAL RULES
            # Locked strictly to the triggers you defined
            DIGITAL_WHITELIST = ["service", "ins/rem", "offline"]
            is_digital_task = False

            if not is_exempt:
                # Rule A: Official Task Type matches whitelist
                if any(trigger in custom_task_type for trigger in DIGITAL_WHITELIST):
                    is_digital_task = True
                # 🌟 Rule B: Boosted Standard contains the word 'digital' (matches 'Premium_Digital')
                elif "digital" in custom_boosted:
                    is_digital_task = True

            # --- 3. ASSIGN STATUS & POOL ---
            t_status = 'ready'
            t_wo = 'none'
            if t['id'] in fresh_sent_db:
                t_status = fresh_sent_db[t['id']].get('status', 'ready').lower()
                t_wo = fresh_sent_db[t['id']].get('wo', 'none')
            
            if stt in config['states']:
                pool.append({
                    "id": t['id'], 
                    "city": addr.get('city', 'Unknown'), 
                    "state": stt,
                    "full": f"{addr.get('number','')} {addr.get('street','')}, {addr.get('city','')}, {stt}",
                    "lat": t['destination']['location'][1], 
                    "lon": t['destination']['location'][0],
                    "escalated": is_esc, 
                    "task_type": tt_val,
                    "is_digital": is_digital_task, # 🔌 Drives the plug icon
                    "db_status": t_status, 
                    "wo": t_wo,
                })
                
        clusters = []
        total_pool = len(pool)
        ic_df = st.session_state.get('ic_df', pd.DataFrame())
        
        # 🌟 CRITICAL FIX: Safe extraction using standardized headers
        lat_col = next((col for col in ic_df.columns if 'lat' in str(col).lower()), 'lat')
        lng_col = next((col for col in ic_df.columns if 'lng' in str(col).lower()), 'lng')
        
        if lat_col in ic_df.columns and lng_col in ic_df.columns:
            v_ics_base = ic_df[~ic_df.astype(str).apply(lambda x: x.str.contains('Field Agent', case=False, na=False).any(), axis=1)].dropna(subset=[lat_col, lng_col]).copy()
        else:
            v_ics_base = pd.DataFrame()

        while pool:
            # Routing progress calculation
            rel_prog = 0.4 + (0.6 * (1 - (len(pool) / total_pool if total_pool > 0 else 1)))
            update_prog(rel_prog, f"🗺️ Routing {len(pool)} remaining tasks...")
            
            anc = pool.pop(0)
            
            # --- NEW: Strict Digital Separation & Dynamic Radius ---
            anc_tt = str(anc.get('task_type', '')).lower()
            anc_is_digital = anc.get('is_digital', False)
            anc_status = anc.get('db_status', 'ready')
            anc_wo = anc.get('wo', 'none')
            
            # Set radius strictly based on the whitelist result
            route_radius = 25 if anc_is_digital else 40
            
            candidates = []; rem = []
            for t in pool:
                t_tt = str(t.get('task_type', '')).lower()
                t_is_digital = t.get('is_digital', False)
                t_status = t.get('db_status', 'ready')
                t_wo = t.get('wo', 'none')
                
                # Rule 1: Digital and Standard never mix
                if anc_is_digital == t_is_digital:
                    
                    # Rule 2: Sent and Accepted are FROZEN
                    # 🌟 FIX 1: Add 'field_nation' so these routes stay grouped together!
                    if anc_status in ['sent', 'accepted', 'field_nation']:
                        # Bypasses distance! ONLY groups if the Work Order matches perfectly.
                        if t_status == anc_status and t_wo == anc_wo:
                            candidates.append((0, t)) 
                        else:
                            rem.append(t)
                            
                    # Rule 3: Ready and Declined are LIQUID (They can mix!)
                    elif anc_status in ['ready', 'declined']:
                        if t_status in ['ready', 'declined']:
                            d = haversine(anc['lat'], anc['lon'], t['lat'], t['lon'])
                            if d <= route_radius: 
                                candidates.append((d, t))
                            else: 
                                rem.append(t)
                        else:
                            rem.append(t)
                else:
                    rem.append(t)
            
            candidates.sort(key=lambda x: x[0])
            
            # --- PRESERVED: 20 STOP LIMIT LOGIC ---
            group = [anc]
            unique_stops = {anc['full']}
            spillover = []
            
            for _, t in candidates:
                # Only add the task if we're under 20 stops OR the task is at an address we already have
                if len(unique_stops) < 20 or t['full'] in unique_stops:
                    group.append(t)
                    unique_stops.add(t['full'])
                else:
                    spillover.append(t)
            
            # 🌟 BRIDGE: Put spillover back and fix the 'd' column error
            rem.extend(spillover)
            
            # --- 📡 1. IC SEARCH & DISTANCE CHECK (OPTIMIZED) ---
            has_ic = False
            ic_dist = 0
            closest_ic_loc = f"{anc['lat']},{anc['lon']}" 
            
            if not v_ics_base.empty:
                # 🚀 OPTIMIZATION: Use list comprehension instead of pandas .apply(). It is ~100x faster.
                dists = [
                    haversine(anc['lat'], anc['lon'], lat, lng) 
                    for lat, lng in zip(v_ics_base[lat_col], v_ics_base[lng_col])
                ]
                
                valid_ics = v_ics_base.copy()
                valid_ics['d'] = dists
                valid_ics = valid_ics[valid_ics['d'] <= 100]
                
                if not valid_ics.empty:
                    best_ic = valid_ics.sort_values('d').iloc[0]
                    has_ic = True
                    ic_dist = best_ic['d']
                    closest_ic_loc = best_ic.get('location', closest_ic_loc)

            def check_viability(grp):
                seen = set(); u_locs = []
                for x in grp:
                    if x['full'] not in seen: seen.add(x['full']); u_locs.append(x['full'])
                if not u_locs: return 0, 0
                
                # 🚀 OPTIMIZATION: Reverted back to real Google Maps!
                # Wrapping u_locs[:25] in a tuple() makes Streamlit's cache process it instantly.
                _, hrs, _ = get_gmaps(closest_ic_loc, tuple(u_locs[:25]))
                pay = round(max(len(u_locs) * 18.0, hrs * 25.0), 2)
                return round(pay / len(u_locs), 2), len(u_locs)
            
            gate_avg, _ = check_viability(group)
            
            # --- 🚦 2. UPDATED FLAGGING LOGIC ---
            if anc_status in ['sent', 'accepted', 'finalized']:
                status = anc_status.capitalize()
            else:
                status = "Ready" # Default status
                
                # Flag Criteria A: High Rate (> $23/stop)
                if gate_avg > 23.00:
                    if len(group) > 1:
                        removed = group.pop()
                        new_avg, _ = check_viability(group)
                        if new_avg <= 23.00:
                            rem.append(removed)
                        else:
                            group.append(removed)
                            status = "Flagged"
                    else:
                        status = "Flagged"
                
                # Flag Criteria B: Long Distance (> 60 miles) or No Contractor
                if not has_ic or ic_dist > 60:
                    status = "Flagged"

            # --- 📊 3. COUNTERS & SAVE TO SESSION ---
            g_data = group

            # 🌟 CLEANUP: No need to loop again; the anchor already knows!
            route_is_digital = anc_is_digital
            
            clusters.append({
                "data": g_data, 
                "center": [anc['lat'], anc['lon']], 
                "stops": len(set(x['full'] for x in g_data)), 
                "city": anc['city'], "state": anc['state'],
                "status": status,
                "has_ic": has_ic,
                "esc_count": sum(1 for x in g_data if x.get('escalated')),
                "is_digital": route_is_digital, # 🔌 Driven by the anchor's verified flag
                "inst_count": sum(1 for x in g_data if "install" in str(x.get('task_type', '')).lower()),
                "remov_count": sum(1 for x in g_data if str(x.get('task_type', '')).lower() in ["kiosk removal", "remove kiosk"]),
                "wo": anc_wo
            })
            pool = rem

        st.session_state[f"clusters_{pod_name}"] = clusters
        if not master_bar: 
            prog_bar.empty()

    except Exception as e:
        st.error(f"Error initializing {pod_name}: {str(e)}")
# 🌟 NEW HELPER: Standardized Digital Badges
def get_digi_badges(cluster_data):
    icons = set()
    for t in cluster_data:
        if t.get('is_digital'):
            tt = str(t.get('task_type', '')).lower()
            if 'offline' in tt: icons.add('📵')
            elif 'ins/re' in tt: icons.add('🔧') # 🌟 Standard Wrench
            else: icons.add('⚙️')
    return "".join(sorted(list(icons)))

# 🌟 NEW HELPER: Groups clusters by State, then sorts them by geographical proximity
def group_and_sort_by_proximity(bucket):
    if not bucket: return []
    grouped = {}
    for c in bucket:
        stt = c.get('state', 'UNKNOWN')
        if stt not in grouped: grouped[stt] = []
        grouped[stt].append(c)
    
    final_list = []
    for stt in sorted(grouped.keys()):
        state_cls = grouped[stt]
        if not state_cls: continue
        
        # Start with the first cluster and chain the nearest neighbors
        sorted_st_cls = [state_cls.pop(0)]
        while state_cls:
            last_center = sorted_st_cls[-1]['center']
            # Find the closest remaining cluster in this state
            closest_idx, min_d = 0, float('inf')
            for idx, x in enumerate(state_cls):
                d = haversine(last_center[0], last_center[1], x['center'][0], x['center'][1])
                if d < min_d:
                    min_d, closest_idx = d, idx
            sorted_st_cls.append(state_cls.pop(closest_idx))
        
        final_list.extend(sorted_st_cls)
    return final_list

# --- DISPATCH RENDERING ---
def render_dispatch(i, cluster, pod_name, is_sent=False, is_declined=False):
    # Capture current state identifiers
    task_ids = [str(t['id']).strip() for t in cluster['data']]
    cluster_hash = hashlib.md5("".join(sorted(task_ids)).encode()).hexdigest()
    sync_key = f"sync_{cluster_hash}"
    real_id = st.session_state.get(sync_key)
    link_id = real_id if real_id else "LINK_PENDING"

    # --- 🛡️ DEFERRED ONFLEET SCRUB ENGINE ---
    scrub_key = f"scrub_timer_{cluster_hash}"
    if scrub_key in st.session_state and time.time() >= st.session_state[scrub_key]:
        with st.spinner("🔍 Validating task availability with Onfleet..."):
            active_tasks = []
            for t in cluster['data']:
                try:
                    # Check if task is still unassigned (state 0)
                    res = requests.get(f"https://onfleet.com/api/v2/tasks/{t['id']}", headers=headers).json()
                    if res.get('state') == 0: active_tasks.append(t)
                except: active_tasks.append(t) # Keep if API fails to be safe
            
            # Update the cluster with valid data only
            cluster['data'] = active_tasks
            cluster['stops'] = len(set(x['full'] for x in active_tasks))
            # Cleanup timer so it only runs once
            st.session_state.pop(scrub_key, None)
            st.toast("🛡️ Route scrubbed: Completed tasks removed.")
            st.rerun()

    # --- 1. STATE KEYS & INITIALIZATION (🌟 UNIQUE BY POD) ---
    pay_key = f"pay_val_{pod_name}_{cluster_hash}"
    rate_key = f"rate_val_{pod_name}_{cluster_hash}"
    sel_key = f"sel_{pod_name}_{cluster_hash}"
    last_sel_key = f"last_sel_{pod_name}_{cluster_hash}"

    st.write("### Route Stops")

    # --- HISTORY LOG ---
    hist = st.session_state.get(f"history_{cluster_hash}", [])
    if hist:
        st.markdown(f"<p style='color: #94a3b8; font-size: 13px; margin-top: -10px; margin-bottom: 15px; font-weight: 600;'>↩️ Previously sent to: {', '.join(hist)}</p>", unsafe_allow_html=True)

    # --- 2. STOP METRICS & PILLS ---
    stop_metrics = {}
    for t in cluster['data']:
        addr = t['full']
        if addr not in stop_metrics:
            stop_metrics[addr] = {
                't_count': 0, 'n_ad': 0, 'c_ad': 0, 'd_ad': 0, 
                'inst': 0, 'remov': 0, 'digi_off': 0, 'digi_ins': 0, 'digi_srv': 0, 
                'custom': {}, 'esc': False # 🌟 REPLACED 'oth' WITH 'custom' DICT
            }
        stop_metrics[addr]['t_count'] += 1
        if t.get('escalated'): stop_metrics[addr]['esc'] = True
            
        raw_tt = str(t.get('task_type', '')).strip()
        parts = [p.strip().lower() for p in raw_tt.split(',') if p.strip()]
        if "escalation" in parts:
            if len(parts) > 1: parts.remove("escalation") 
            else: parts = ["new ad"] 
        tt = ", ".join(parts)

        found_category = False
        
        # 🌟 Split Digital Tasks
        if t.get('is_digital'):
            if "offline" in tt: stop_metrics[addr]['digi_off'] += 1
            elif "ins/re" in tt: stop_metrics[addr]['digi_ins'] += 1
            else: stop_metrics[addr]['digi_srv'] += 1
            found_category = True
        else:
            if "install" in tt: 
                stop_metrics[addr]['inst'] += 1
                found_category = True
            if any(trigger in tt for trigger in ["kiosk removal", "remove kiosk"]):
                stop_metrics[addr]['remov'] += 1
                found_category = True
            if any(x in tt for x in ["continuity", "photo retake", "swap"]): 
                stop_metrics[addr]['c_ad'] += 1
                found_category = True
            if any(x in tt for x in ["default", "pull down"]): 
                stop_metrics[addr]['d_ad'] += 1
                found_category = True
        
        if any(x in tt for x in ["new ad", "art change", "top"]) or not tt:
            stop_metrics[addr]['n_ad'] += 1
        elif not found_category:
            # 🌟 THE FIX: Push exactly the remaining task type over
            display_tt = tt.title()
            if display_tt not in stop_metrics[addr]['custom']:
                stop_metrics[addr]['custom'][display_tt] = 0
            stop_metrics[addr]['custom'][display_tt] += 1
            
    # --- UI RENDERING (WITH BREAK-OFF FEATURE) ---
    for addr, metrics in stop_metrics.items():
        pill_parts = []
        if metrics['n_ad'] > 0: pill_parts.append(f"🆕 {metrics['n_ad']} New Ad")
        if metrics['c_ad'] > 0: pill_parts.append(f"🔄 {metrics['c_ad']} Continuity")
        if metrics['d_ad'] > 0: pill_parts.append(f"⚪ {metrics['d_ad']} Default")
        if metrics['inst'] > 0: pill_parts.append(f"🛠️ {metrics['inst']} Kiosk Install")
        if metrics['remov'] > 0: pill_parts.append(f"🗑️ {metrics['remov']} Kiosk Removal")
        
        # 🌟 Render Custom Task Types dynamically
        for custom_name, count in metrics['custom'].items():
            pill_parts.append(f"📋 {count} {custom_name}")
            
        # 🌟 Inject Digital Icons
        if metrics['digi_off'] > 0: pill_parts.append(f"📵 {metrics['digi_off']} Offline")
        if metrics['digi_ins'] > 0: pill_parts.append(f"🔧 {metrics['digi_ins']} Ins/Rem")
        if metrics['digi_srv'] > 0: pill_parts.append(f"⚙️ {metrics['digi_srv']} Service")
        
        pill_str = " | ".join(pill_parts)
        display_addr = f"⭐ {addr}" if metrics['esc'] else addr
        
        # UI: Stop Info + Break-Off Button Layout
        s_col, b_col = st.columns([0.9, 0.1], vertical_alignment="center")
        with s_col:
            st.markdown(
                f"<b>{display_addr}</b> &nbsp;"
                f"<span style='color: #633094; background-color: #f3e8ff; padding: 2px 6px; border-radius: 10px; font-weight: 800; font-size: 11px;'>"
                f"{metrics['t_count']} Tasks</span>&nbsp; "
                f"<span style='font-size: 13px; color: #475569;'>— {pill_str}</span>", 
                unsafe_allow_html=True
            )
        with b_col:
            if not is_sent and not is_declined:
                # 🌟 THE BREAK-OFF TOOL (UNIQUE KEY)
                if st.button("✂️", key=f"split_{pod_name}_{cluster_hash}_{hashlib.md5(addr.encode()).hexdigest()[:6]}", help=f"Remove this stop from the route"):
                    # 1. Identify tasks to move
                    tasks_to_move = [t for t in cluster['data'] if t['full'] == addr]
                    
                    # 2. Create the new "Fragment" route
                    new_fragment = {
                        "data": tasks_to_move, 
                        "center": [tasks_to_move[0]['lat'], tasks_to_move[0]['lon']], 
                        "stops": 1, 
                        "city": tasks_to_move[0]['city'], 
                        "state": tasks_to_move[0]['state'],
                        "status": "Ready",
                        "has_ic": cluster.get('has_ic', False),
                        "esc_count": sum(1 for x in tasks_to_move if x.get('escalated')),
                        "is_digital": any(x.get('is_digital') for x in tasks_to_move),
                        "inst_count": sum(1 for x in tasks_to_move if "install" in str(x.get('task_type', '')).lower()),
                        "remov_count": sum(1 for x in tasks_to_move if "remove" in str(x.get('task_type', '')).lower()),
                        "wo": "none"
                    }

                    # 3. Remove from current cluster
                    cluster['data'] = [t for t in cluster['data'] if t['full'] != addr]
                    cluster['stops'] = len(set(t['full'] for t in cluster['data']))
                    
                    # 4. Inject new route into Pod memory (Determine target pod)
                    target_pod = pod_name if pod_name != "Global_Digital" else next((p for p, cfg in POD_CONFIGS.items() if new_fragment['state'] in cfg['states']), "UNKNOWN")
                    if target_pod != "UNKNOWN" and f"clusters_{target_pod}" in st.session_state:
                        st.session_state[f"clusters_{target_pod}"].append(new_fragment)
                    
                    # 5. Clear pricing for parent route to force recalculation
                    st.session_state.pop(pay_key, None)
                    st.session_state.pop(rate_key, None)
                    
                    st.toast(f"📍 Stop broken off into a standalone route!")
                    st.rerun()
        
    st.divider()

    # --- 3. CONTRACTOR FILTERING (100 MILES) ---
    ic_df = st.session_state.get('ic_df', pd.DataFrame())
    ic_opts = {} 
    v_ics = pd.DataFrame() 

    if not ic_df.empty:
        ic_df.columns = [str(c).strip().lower() for c in ic_df.columns]
        lat_col, lng_col = 'lat', 'lng'
        if lat_col in ic_df.columns and lng_col in ic_df.columns:
            v_ics = ic_df[~ic_df.astype(str).apply(lambda x: x.str.contains('Field Agent', case=False, na=False).any(), axis=1)].copy()
            v_ics = v_ics.dropna(subset=[lat_col, lng_col])
            if not v_ics.empty:
                v_ics['d'] = v_ics.apply(lambda x: haversine(cluster['center'][0], cluster['center'][1], x[lat_col], x[lng_col]), axis=1)
                v_ics = v_ics[v_ics['d'] <= 100].sort_values('d')
                for _, r in v_ics.iterrows():
                    cert_val = str(r.get('digital certified', '')).strip().upper()
                    cert_icon = " 🔌" if cert_val in ['YES', 'Y', 'TRUE', '1', '1.0'] else ""
                    ic_name = r.get('name', 'Unknown')
                    label = f"{ic_name}{cert_icon} ({round(r['d'], 1)} mi)"
                    ic_opts[label] = r

    # --- DYNAMIC PRICING SYNC ---
    def sync_on_total():
        val = st.session_state.get(pay_key)
        if val is not None:
            st.session_state[rate_key] = round(val / cluster['stops'], 2) if cluster['stops'] > 0 else 0

    def sync_on_rate():
        val = st.session_state.get(rate_key)
        if val is not None:
            st.session_state[pay_key] = round(val * cluster['stops'], 2)

    def update_for_new_contractor():
        selected_label = st.session_state.get(sel_key)
        if selected_label and selected_label != st.session_state.get(last_sel_key):
            ic_new = ic_opts[selected_label]
            _, h, _ = get_gmaps(ic_new.get('location', f"{cluster['center'][0]},{cluster['center'][1]}"), tuple(stop_metrics.keys()))
            new_pay = float(round(max(cluster['stops'] * 18.0, h * 25.0), 2))
            st.session_state[pay_key] = new_pay
            st.session_state[rate_key] = round(new_pay / cluster['stops'], 2) if cluster['stops'] > 0 else 0
            st.session_state[last_sel_key] = selected_label

    # --- 4. INITIAL SETUP (FIXED SAVING LOGIC) ---
    if pay_key not in st.session_state:
        prev_name = cluster.get('contractor_name', 'Unknown')
        default_label = list(ic_opts.keys())[0] if ic_opts else None
        
        # Match previous contractor if possible
        if prev_name != 'Unknown' and ic_opts:
            for label, row in ic_opts.items():
                if row.get('name') == prev_name:
                    default_label = label; break

        if default_label:
            ic_init = ic_opts[default_label]
            _, h, _ = get_gmaps(ic_init.get('location', f"{cluster['center'][0]},{cluster['center'][1]}"), tuple(stop_metrics.keys()))
            # Floor calculation: Max of $18/stop or $25/hr
            initial_pay = float(round(max(cluster['stops'] * 18.0, h * 25.0), 2))
            st.session_state[sel_key] = default_label
            st.session_state[last_sel_key] = default_label
        else:
            # Fallback floor if no IC is found
            initial_pay = float(round(cluster['stops'] * 18.0, 2))

        # 🌟 THE FIX: Save the calculated pay OUTSIDE the if/else so it always saves
        st.session_state[pay_key] = initial_pay
        st.session_state[rate_key] = round(initial_pay / cluster['stops'], 2) if cluster['stops'] > 0 else 18.0
    
    # --- 4. UI RENDERING & BUTTON LOGIC ---
    route_state = st.session_state.get(f"route_state_{cluster_hash}")
    is_fn = (route_state == "field_nation") # 🌟 MOVED UP to control the Contractor field
    
    col_a, col_b, col_c, col_d = st.columns([1.5, 1, 1, 1])
    with col_a:
        # 🌟 THE FIX: If assigned to FN, lock the field. Otherwise, show the dropdown!
        if is_fn:
            ic = {"name": "Field Nation", "location": f"{cluster['center'][0]},{cluster['center'][1]}", "d": 0}
            st.markdown("<div style='background:#FEF9C3; border:1px solid #EAB308; border-radius:8px; padding:10px; text-align:center; height:42px; display:flex; align-items:center; justify-content:center;'><span style='color:#854D0E; font-weight:800; font-size:14px;'>🌐 Field Nation</span></div>", unsafe_allow_html=True)
        elif ic_opts:
            selected_label = st.selectbox("Select IC", list(ic_opts.keys()), key=sel_key, on_change=update_for_new_contractor)
            ic = ic_opts[selected_label]
        else:
            ic = {"name": "Manual/FN", "location": f"{cluster['center'][0]},{cluster['center'][1]}", "d": 0}
            st.info("No ICs within 100mi.")

    st.divider()
    
    # --- 🌐 FIELD NATION PERSISTENCE (CHECKBOX) ---
    
    if route_state != "email_sent":
        # 🌟 UNIQUE KEY
        fn_checked = st.checkbox("🌐 Assign to Field Nation", value=is_fn, key=f"fn_check_{pod_name}_{cluster_hash}")
        
        if fn_checked and not is_fn:
            with st.spinner("Pushing to Google Sheet..."):
                home = ic.get('location', f"{cluster['center'][0]},{cluster['center'][1]}")
                payload = {
                    "cluster_hash": cluster_hash,
                    "icn": "Field Nation", 
                    "city": cluster.get('city', 'Unknown'), 
                    "state": cluster.get('state', 'Unknown'), 
                    "taskIds": ",".join(task_ids),
                    "wo": f"FN-{datetime.now().strftime('%m%d%Y')}",
                    "lCnt": cluster['stops'],
                    "tCnt": len(task_ids),
                    "locs": " | ".join([home] + list(stop_metrics.keys()) + [home])
                }
                try:
                    res = requests.post(GAS_WEB_APP_URL, json={"action": "saveToFieldNation", "payload": payload}, timeout=10).json()
                    if res.get("success"):
                        st.session_state[f"route_state_{cluster_hash}"] = "field_nation"
                        st.toast("✅ Saved to Field Nation Tab")
                        st.rerun()
                    else:
                        st.error(f"Sheet Error: {res.get('error')}")
                except Exception as e:
                    st.error(f"Connection Failed: {e}")
        
        elif not fn_checked and is_fn:
            # 🌟 ADDED Safety check for Field Nation revocation
            with st.popover("🚨 Confirm Field Nation Revocation", use_container_width=True):
                st.error("Remove this route from Field Nation tracking?")
                # 🌟 THE FIX: Upgraded to a callback so it doesn't freeze the screen!
                st.button("🚨 Yes, Revoke FN", key=f"fn_rev_confirm_{pod_name}_{cluster_hash}", type="primary", use_container_width=True, on_click=move_to_dispatch, kwargs={"cluster_hash": cluster_hash, "ic_name": "Field Nation", "pod_name": pod_name, "action_label": "Field Nation Revoked", "check_onfleet": True})
            st.stop()

    BG_COLOR = "#FEF9C3"
    TEXT_COLOR = "#854D0E"
    BORDER_COLOR = "#FACC15"

    if route_state == "field_nation":
        st.info("💡 Route is currently tracked in the Field Nation tab.")
        # 🌟 UNIQUE KEY
        if st.button("📢 Mark as Posted (Move to Sent)", key=f"posted_{pod_name}_{cluster_hash}", type="primary", use_container_width=True):
            with st.spinner("Moving route to Sent database..."):
                try:
                    res = requests.post(GAS_WEB_APP_URL, json={"action": "postFieldNationRoute", "cluster_hash": cluster_hash}, timeout=10).json()
                    if res.get("success"):
                        st.session_state[f"route_state_{cluster_hash}"] = "email_sent"
                        st.session_state[f"contractor_{cluster_hash}"] = "Field Nation"
                        st.session_state[f"sent_ts_{cluster_hash}"] = datetime.now().strftime('%m/%d %I:%M %p')
                        st.session_state[f"sync_{cluster_hash}"] = res.get("routeId") 
                        st.toast("🚀 Moved to Sent in Google Sheets!")
                        st.rerun()
                    else:
                        st.error(f"Sheet Error: {res.get('error')}")
                except Exception as e:
                    st.error(f"Connection Failed: {e}")

    ic_location = ic.get('location', f"{cluster['center'][0]},{cluster['center'][1]}")
    mi, hrs, t_str = get_gmaps(ic_location, tuple(stop_metrics.keys()))
    
    curr_rate = st.session_state[rate_key]
    ic_dist = ic.get('d', 0)
    needs_unlock = (curr_rate >= 25.0) or (ic_dist > 60) or (cluster['status'] == 'Flagged')
    is_unlocked = True 
    
    if needs_unlock:
        reasons = []
        if curr_rate >= 25.0: reasons.append(f"High Rate (${curr_rate})")
        if ic['d'] > 60: reasons.append(f"Distance ({round(ic['d'],1)}mi)")
        if cluster['status'] == 'Flagged': reasons.append("Flagged Route")
        st.markdown(f"""<div style="background-color:#fef2f2; border:1px solid #ef4444; padding:10px; border-radius:8px; margin-bottom:15px;"><span style="color:#b91c1c; font-weight:800;">🔒 ACTION REQUIRED:</span> <span style="color:#7f1d1d;">{" & ".join(reasons)}</span></div>""", unsafe_allow_html=True)
        # 🌟 UNIQUE KEY
        is_unlocked = st.checkbox("Authorize Premium Rate / Distance", key=f"lock_{pod_name}_{cluster_hash}")

    with col_b:
        st.number_input("Total Comp ($)", min_value=0.0, step=5.0, key=pay_key, on_change=sync_on_total, disabled=not is_unlocked)
    with col_c:
        st.number_input("Rate/Stop ($)", min_value=0.0, step=1.0, key=rate_key, on_change=sync_on_rate, disabled=not is_unlocked)
    with col_d:
        # 🌟 UNIQUE KEY
        st.date_input("Deadline", datetime.now().date()+timedelta(14), key=f"dd_{pod_name}_{cluster_hash}", disabled=not is_unlocked)

    # --- 6. UPDATED FINANCIALS & PREVIEW ---
    final_pay = st.session_state.get(pay_key, 0.0)
    final_rate = st.session_state.get(rate_key, 0.0)

    m1, m2 = st.columns(2)
    with m1: 
        status_color = TB_GREEN if 18.0 <= final_rate <= 23.0 else "#ef4444"
        st.markdown(f"<div style='background:#ffffff; border:1px solid #cbd5e1; border-radius:12px; padding:15px; margin-bottom:10px;'><p style='font-size:11px; font-weight:800; text-transform:uppercase;'>Financials</p><p style='margin:0; font-size:24px; font-weight:800; color:{status_color};'>Total: ${final_pay:,.2f}</p><p style='margin:0; font-size:13px;'>Breakdown: ${final_rate}/stop</p></div>", unsafe_allow_html=True)
    with m2: 
        st.markdown(f"<div style='background:#ffffff; border:1px solid #cbd5e1; border-radius:12px; padding:15px; margin-bottom:10px;'><p style='font-size:11px; font-weight:800; text-transform:uppercase;'>Logistics</p><p style='margin:0; font-size:24px; font-weight:800;'>{t_str}</p><p style='margin:0; font-size:13px;'>Round Trip: {mi} mi</p></div>", unsafe_allow_html=True)
    stops_text = ""
    for i, (addr, metrics) in enumerate(list(stop_metrics.items())[:2], start=1):
        esc_star = "⭐ " if metrics['esc'] else ""
        stops_text += f"📍 Stop {i}: {esc_star}{addr}\n"
        
    if len(stop_metrics) > 2:
        stops_text += f"   ... and {len(stop_metrics) - 2} more stops.\n"

    loc_pills = {}
    for t in cluster['data']:
        addr = t.get('full', 'Unknown')
        if addr not in loc_pills: loc_pills[addr] = ""
        if t.get('escalated') and "⭐" not in loc_pills[addr]: loc_pills[addr] += "⭐"
        
        # 🌟 THE FIX: Split Digital Email Output
        if t.get('is_digital'):
            tt_lower = str(t.get('task_type','')).lower()
            if "offline" in tt_lower and "📵" not in loc_pills[addr]: loc_pills[addr] += "🔌"
            elif "ins/re" in tt_lower and "🔧" not in loc_pills[addr]: loc_pills[addr] += "🔧"
            elif ("offline" not in tt_lower and "ins/re" not in tt_lower) and "⚙️" not in loc_pills[addr]: 
                loc_pills[addr] += "⚙️"
        else:
            if "install" in str(t.get('task_type','')).lower() and "🛠️" not in loc_pills[addr]: loc_pills[addr] += "🛠️"
            if str(t.get('task_type','')).lower() in ["kiosk removal", "remove kiosk"] and "🗑️" not in loc_pills[addr]: 
                loc_pills[addr] += "🗑️"

    due = st.session_state.get(f"dd_{pod_name}_{cluster_hash}", datetime.now().date()+timedelta(14))
    is_already_sent = is_sent or is_declined or st.session_state.get(f"route_state_{cluster_hash}") == "email_sent"
    
    prev_ic_name = cluster.get('contractor_name', 'Unknown')
    ic_name = ic.get('name', 'Unknown Contractor') 
    
    if ic_name == prev_ic_name and cluster.get('wo', 'none') != 'none':
        wo_val = cluster['wo']
    else:
        wo_val = f"{ic.get('name', 'Unknown')}-{datetime.now().strftime('%m%d%Y')}"
    # 🌟 NEW: Calculate route-level task breakdowns for the email preview
    route_task_counts = {}
    total_installs = 0
    
    for t in cluster['data']:
        raw_tt = str(t.get('task_type', '')).strip()
        clean_tt_lower = raw_tt.lower().replace("escalation", "").replace("  ", " ").strip(" ,-|:")
        
        # Default to New Ad if empty
        if not clean_tt_lower:
            clean_tt_lower = "new ad"
            display_tt = "New Ad"
        else:
            display_tt = clean_tt_lower.title()

        is_digi = t.get('is_digital')
        category = None
        
        # 🚦 Match exactly to the UI buckets
        if is_digi:
            if "offline" in clean_tt_lower: category = "📵 Offline"
            elif "ins/re" in clean_tt_lower: category = "🔧 Ins/Rem"
            else: category = "⚙️ Service"
        else:
            if "install" in clean_tt_lower: 
                category = "🛠️ Kiosk Install"
                total_installs += 1
            elif any(x in clean_tt_lower for x in ["kiosk removal", "remove kiosk"]): category = "🗑️ Kiosk Removal"
            elif any(x in clean_tt_lower for x in ["continuity", "photo retake", "swap"]): category = "🔄 Continuity"
            elif any(x in clean_tt_lower for x in ["default", "pull down"]): category = "⚪ Default"
            elif any(x in clean_tt_lower for x in ["new ad", "art change", "top"]): category = "🆕 New Ad"
            else: category = f"📋 {display_tt}" # Pass custom types straight through
            
        if category not in route_task_counts:
            route_task_counts[category] = 0
        route_task_counts[category] += 1

    # Format the breakdown list cleanly for the email
    task_breakdown_str = "\n".join([f"  {cat}: {count}" for cat, count in route_task_counts.items()]) + "\n"
    
    install_warning = f"\n⚠️ NOTE: This route contains Kiosk Installs. Please ensure you have adequate storage and vehicle space.\n" if total_installs > 0 else ""
    
    sig_preview = (
        f"Hello {ic.get('name', 'Contractor')},\n\n"
        f"We have a new route available for you to review.\n\n"
        f" Work Order: {wo_val}\n"
        f"📅 Due Date: {due.strftime('%A, %b %d, %Y')}\n"
        f" Total Stops: {cluster['stops']}\n"
        f" Estimated Compensation: ${final_pay:.2f}\n\n"
        f" Task Breakdown:\n"
        f"{task_breakdown_str}"
        f"{install_warning}\n"
        f"To view the complete route details—including total stops, estimated mileage, and time—please click the secure link below to access your Route Summary.\n\n"
        f"⚠️ ACTION REQUIRED:\n"
        f"You must confirm by selecting 'Accept' or 'Decline' directly through the portal link.\n\n"
        f"Route Summary Link:\n"
        f"{PORTAL_BASE_URL}?route={link_id}&v2=true"
    )
    
    # 🌟 UNIQUE KEY
    last_data_key = f"last_data_{pod_name}_{cluster_hash}"
    version_key = f"tx_ver_{pod_name}_{cluster_hash}"
    current_data_fingerprint = f"{ic.get('name', 'Unknown')}_{final_pay}_{due}_{wo_val}"
    
    if version_key not in st.session_state:
        st.session_state[version_key] = 1

    if st.session_state.get(last_data_key) != current_data_fingerprint:
        st.session_state[version_key] += 1
        st.session_state[last_data_key] = current_data_fingerprint
        st.session_state[f"tx_{pod_name}_{cluster_hash}_{st.session_state[version_key]}"] = sig_preview
    
    active_tx_key = f"tx_{pod_name}_{cluster_hash}_{st.session_state[version_key]}"

    if active_tx_key not in st.session_state:
        st.session_state[active_tx_key] = sig_preview
    elif real_id and "LINK_PENDING" in st.session_state[active_tx_key]:
        st.session_state[active_tx_key] = st.session_state[active_tx_key].replace("LINK_PENDING", real_id)
    
   # 🌟 UNIQUE KEY & PERFECT INDENTATION
    email_body_content = st.text_area("Email Content Preview", value=sig_preview, height=180, key=f"txt_area_{pod_name}_{current_data_fingerprint}_{cluster_hash}", disabled=not is_unlocked)

    # --- HIGH-SPEED DISPATCH BUTTON ---
    btn_label = "✉️ RESEND LINK & OPEN GMAIL" if is_already_sent else "🚀 GENERATE LINK & OPEN GMAIL"

    if st.button(btn_label, type="primary", key=f"gbtn_{pod_name}_{cluster_hash}", disabled=not is_unlocked, use_container_width=True):
        # 🛡️ STEP 1: FAST COLLISION CHECK (Memory-based)
        local_sent_db = st.session_state.get('sent_db', {})
        collision = next((tid for tid in task_ids if tid in local_sent_db), None)
        
        # 🌟 THE FIX: Only block collisions if we are trying to send a brand new route!
        if collision and not is_already_sent:
            st.error(f"🚫 COLLISION: Dispatched by someone else ({local_sent_db[collision]['name']}).")
            st.rerun() 
            return

        # 🚀 STEP 2: PROCEED WITH DISPATCH
        with st.spinner("Generating link..."):
            home = ic.get('location', f"{cluster['center'][0]},{cluster['center'][1]}")
            payload = {
                "cluster_hash": cluster_hash,
                "icn": ic.get('name', 'Unknown'), 
                "ice": ic.get('email', ''), 
                "wo": wo_val, 
                "due": str(due), "comp": final_pay, "lCnt": cluster['stops'], "mi": mi, "time": t_str, 
                "phone": str(ic.get('phone', '')),
                "locs": " | ".join([home] + list(stop_metrics.keys()) + [home]),
                "taskIds": ",".join(task_ids),
                "tCnt": len(task_ids),
                "jobOnly": " | ".join([f"{addr} {pills}" for addr, pills in loc_pills.items()])
            }

            try:
                res = requests.post(GAS_WEB_APP_URL, json={"action": "saveRoute", "payload": payload}, timeout=10).json()
            except Exception as e:
                st.error(f"Connection Error: {e}")
                st.stop()
            
            if res.get("success"):
                # 3. 🧠 UPDATE STATE INSTANTLY
                final_route_id = res.get("routeId")
                st.session_state[sync_key] = final_route_id
                st.session_state[f"sent_ts_{cluster_hash}"] = datetime.now().strftime('%m/%d %I:%M %p')
                st.session_state[f"contractor_{cluster_hash}"] = ic.get('name', 'Unknown')
                st.session_state[f"wo_{cluster_hash}"] = wo_val  # 🌟 NEW: Save WO to memory!
                st.session_state[f"route_state_{cluster_hash}"] = "email_sent"
                st.session_state[f"reverted_{cluster_hash}"] = False
                
                # 4. ✉️ TRIGGER GMAIL WINDOW
                final_sig = email_body_content.replace("LINK_PENDING", final_route_id)
                subject_line = requests.utils.quote(f"Route Request | {wo_val}")
                body_content = requests.utils.quote(final_sig)
                gmail_url = f"https://mail.google.com/mail/?view=cm&fs=1&to={ic.get('email', '')}&su={subject_line}&body={body_content}"
                st.components.v1.html(f"<script>window.open('{gmail_url}', '_blank');</script>", height=0)
                
                # 5. ⏳ VISUAL COUNTDOWN & MOVE (Restored)
                timer_placeholder = st.empty()
                for seconds in range(2, 0, -1):
                    timer_placeholder.success(f"✅ Link Live! Moving to 'Sent' in {seconds}s...")
                    time.sleep(1)
                timer_placeholder.empty()
                st.rerun()
                    
def run_pod_tab(pod_name):
    # Grab the contractor database from session state
    ic_df = st.session_state.get('ic_df', pd.DataFrame())
    
    # Grab the matching "Midnight" text color for the current pod
    text_color = {
        "Blue": "#1e3a8a", "Green": "#064e3b", "Orange": "#7c2d12",
        "Purple": "#4c1d95", "Red": "#7f1d1d"
    }.get(pod_name, "#633094")
    
    # Check if data exists for this pod to determine button state
    is_initialized = f"clusters_{pod_name}" in st.session_state
    
    # 🌟 HEADER ROW: Title Centered, Dynamic Button Top Right
    h_col1, h_col2, h_col3 = st.columns([2, 6, 2])
    with h_col2:
        st.markdown(f"<h2 style='color: {text_color}; text-align:center; margin-top: 0;'>{pod_name} Pod Dashboard</h2>", unsafe_allow_html=True)
    with h_col3:
        st.markdown("<div class='tab-action-btn'>", unsafe_allow_html=True)
        if not is_initialized:
            # STATE 1: Not loaded yet
            if st.button(f"🚀 Initialize Data", key=f"init_{pod_name}", use_container_width=True):
                process_pod(pod_name)
                st.rerun()
        else:
            # STATE 2: Loaded (Replaces the old Re-Optimize button)
            if st.button("🚀 Sync Routes", key=f"reopt_{pod_name}", use_container_width=True):
                st.session_state.pop(f"clusters_{pod_name}", None)
                process_pod(pod_name) 
                st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)

    # 🌟 THE FIX: Remove the early return and safely default to an empty list
    # Load cluster data safely so the Supercards can render 0's
    cls = st.session_state.get(f"clusters_{pod_name}", [])

    # --- KEEPING THE CLEAN AUTO-SYNC LOGIC ---
    sent_db, ghost_db = fetch_sent_records_from_sheet()
    
    # 🌟 THE FIX: Omni-Ghost Sorter
    pod_ghosts, finalized_ghosts, sent_ghosts = [], [], []
    for g in ghost_db.get(pod_name, []):
        g_stat = g.get("status", "")
        local_override = st.session_state.get(f"route_state_{g.get('hash')}")
        if local_override == "finalized" or g_stat == "finalized": finalized_ghosts.append(g)
        elif g_stat == "sent": sent_ghosts.append(g)
        else: pod_ghosts.append(g)

    # 1. 📂 DEFINE BUCKETS
    ready, review, sent, accepted, declined, finalized, field_nation, digital_ready = [], [], [], [], [], [], [], []
    live_hashes = set() # 🌟 Track live routes so we don't duplicate them!

    for c in cls:
        # 🌟 FIX: Skip empty routes that were trimmed to 0 stops
        if not c.get('data') or len(c.get('data')) == 0:
            continue
            
        task_ids = [str(t['id']).strip() for t in c['data']]
        cluster_hash = hashlib.md5("".join(sorted(task_ids)).encode()).hexdigest()
        live_hashes.add(cluster_hash) # Save hash
        
        sheet_match = sent_db.get(next((tid for tid in task_ids if tid in sent_db), None))
        route_state = st.session_state.get(f"route_state_{cluster_hash}")
        local_ts = st.session_state.get(f"sent_ts_{cluster_hash}", "")
        local_contractor = st.session_state.get(f"contractor_{cluster_hash}", "Unknown")
        local_wo = st.session_state.get(f"wo_{cluster_hash}", local_contractor) # 🌟 Fetch WO
        is_reverted = st.session_state.get(f"reverted_{cluster_hash}", False)
        
        if sheet_match and not is_reverted:
            c['contractor_name'] = sheet_match.get('name', 'Unknown')
            c['route_ts'] = sheet_match.get('time', '') or local_ts
            c['wo'] = sheet_match.get('wo', c['contractor_name'])
            c['comp'] = sheet_match.get('comp', 0)    # 🌟 NEW
            c['due'] = sheet_match.get('due', 'N/A')  # 🌟 NEW
        else:
            # 🌟 Apply Fallbacks Instantly
            c['contractor_name'] = local_contractor
            c['wo'] = local_wo
            c['route_ts'] = local_ts
        
        # --- 🚦 THE NEW DIGITAL FLOW ---
        if c.get('is_digital') and not sheet_match and route_state != "email_sent" and not is_reverted:
            digital_ready.append(c)
            continue 

        # --- PRIORITY: LIVE DATABASE OVERRIDES LOCAL STATE ---
        # 🌟 THE FIX: If we just clicked Finalize, override the Google Sheet instantly!
        if route_state == "finalized":
            finalized.append(c)
        elif sheet_match and not is_reverted:
            raw_status = str(sheet_match.get('status', '')).lower()
            if raw_status == 'field_nation': field_nation.append(c) #
            elif raw_status == 'declined': declined.append(c) #
            elif raw_status == 'accepted': accepted.append(c) #
            elif raw_status == 'finalized': finalized.append(c) #
            else: sent.append(c) #
        
        # 🌟 Handle Local Session State (Instant UI Moves)
        elif route_state == "email_sent" and not is_reverted:
            sent.append(c) #
        elif route_state == "field_nation" and not is_reverted: 
            field_nation.append(c) #
        else:
            # Fallback to calculated status
            if c.get('status') == 'Ready': ready.append(c) #
            else: review.append(c) #

    # --- 📊 CATEGORIZED MATH ---
    # Routes
    ready_count = len(ready)
    flagged_count = len(review)
    
    # 🌟 THE FIX: Combine active buckets (Excludes Accepted & Finalized)
    active_cls = ready + review + sent + declined + field_nation + digital_ready
    
    # Tasks
    tasks_static = sum(len(c['data']) for c in active_cls if not c.get('is_digital'))
    tasks_digital = sum(len(c['data']) for c in active_cls if c.get('is_digital'))
    
    # Stops
    stops_static = sum(c['stops'] for c in active_cls if not c.get('is_digital'))
    stops_digital = sum(c['stops'] for c in active_cls if c.get('is_digital'))
    
    # Sent Records
    accepted_count = len(accepted) + len(pod_ghosts)
    declined_count = len(declined)
    total_sent = len(sent) + accepted_count + declined_count + len(field_nation)

    # --- DASHBOARD SUPERCARDS (Standardized 4-Card Layout) ---
    c1, c2, c3, c4 = st.columns([1, 1, 1, 1]) 

    with c1:
        # CARD 1: ROUTE STATUS (Ready | Flagged)
        st.markdown(f"""
            <div class='dashboard-supercard' style='background:#ffffff; border:1px solid #cbd5e1; border-radius:12px; padding:12px; height: 120px;'>
                <p style='margin:0 0 10px 0; font-size:11px; font-weight:800; color:#64748b; text-transform:uppercase; text-align:center;'>Route Status</p>
                <div style='display:flex; justify-content:space-around; align-items:center; gap:8px;'>
                    <div style='background:{TB_GREEN_FILL}; flex:1; padding:8px; border-radius:8px; text-align:center;'>
                        <p style='margin:0; font-size:9px; font-weight:800; color:{TB_GREEN_TEXT};'>READY</p>
                        <p style='margin:0; font-size:24px; font-weight:800; color:{TB_GREEN_TEXT};'>{ready_count}</p>
                    </div>
                    <div style='background:{TB_RED_FILL}; flex:1; padding:8px; border-radius:8px; text-align:center;'>
                        <p style='margin:0; font-size:9px; font-weight:800; color:{TB_RED_TEXT};'>FLAGGED</p>
                        <p style='margin:0; font-size:24px; font-weight:800; color:{TB_RED_TEXT};'>{flagged_count}</p>
                    </div>
                </div>
            </div>
        """, unsafe_allow_html=True)

    with c2:
        # CARD 2: STATIC WORKLOAD (Tasks | Stops)
        st.markdown(f"""
            <div class='dashboard-supercard' style='background:#ffffff; border:1px solid #cbd5e1; border-radius:12px; padding:12px; height: 120px;'>
                <p style='margin:0 0 10px 0; font-size:11px; font-weight:800; color:#64748b; text-transform:uppercase; text-align:center;'>Static Workload</p>
                <div style='display:flex; justify-content:space-around; align-items:center; gap:8px;'>
                    <div style='background:{TB_STATIC_FILL}; flex:1; padding:8px; border-radius:8px; text-align:center;'>
                        <p style='margin:0; font-size:9px; font-weight:800; color:{TB_STATIC_TEXT};'>TASKS</p>
                        <p style='margin:0; font-size:24px; font-weight:800; color:{TB_STATIC_TEXT};'>{tasks_static}</p>
                    </div>
                    <div style='background:{TB_STATIC_FILL}; flex:1; padding:8px; border-radius:8px; text-align:center;'>
                        <p style='margin:0; font-size:9px; font-weight:800; color:{TB_STATIC_TEXT};'>STOPS</p>
                        <p style='margin:0; font-size:24px; font-weight:800; color:{TB_STATIC_TEXT};'>{stops_static}</p>
                    </div>
                </div>
            </div>
        """, unsafe_allow_html=True)

    with c3:
        # CARD 3: DIGITAL WORKLOAD (Updated to Static Theme)
        st.markdown(f"""
            <div class='dashboard-supercard' style='background:#ffffff; border:1px solid #cbd5e1; border-radius:12px; padding:12px; height: 120px;'>
                <p style='margin:0 0 10px 0; font-size:11px; font-weight:800; color:#64748b; text-transform:uppercase; text-align:center;'>Digital Workload</p>
                <div style='display:flex; justify-content:space-around; align-items:center; gap:8px;'>
                    <div style='background:{TB_STATIC_FILL}; flex:1; padding:8px; border-radius:8px; text-align:center;'>
                        <p style='margin:0; font-size:9px; font-weight:800; color:{TB_STATIC_TEXT};'>TASKS</p>
                        <p style='margin:0; font-size:24px; font-weight:800; color:{TB_STATIC_TEXT};'>{tasks_digital}</p>
                    </div>
                    <div style='background:{TB_STATIC_FILL}; flex:1; padding:8px; border-radius:8px; text-align:center;'>
                        <p style='margin:0; font-size:9px; font-weight:800; color:{TB_STATIC_TEXT};'>STOPS</p>
                        <p style='margin:0; font-size:24px; font-weight:800; color:{TB_STATIC_TEXT};'>{stops_digital}</p>
                    </div>
                </div>
            </div>
        """, unsafe_allow_html=True)

    with c4:
        # CARD 4: SENT RECORDS (Accepted | Declined)
        st.markdown(f"""
            <div class='dashboard-supercard' style='background:#ffffff; border:1px solid #cbd5e1; border-radius:12px; padding:12px; height: 120px;'>
                <p style='margin:0 0 10px 0; font-size:11px; font-weight:800; color:#64748b; text-transform:uppercase; text-align:center;'>Sent: {total_sent}</p>
                <div style='display:flex; justify-content:space-around; align-items:center; gap:8px;'>
                    <div style='background:{TB_GREEN_FILL}; flex:1; padding:8px; border-radius:8px; text-align:center;'>
                        <p style='margin:0; font-size:9px; font-weight:800; color:{TB_GREEN_TEXT};'>ACCEPTED</p>
                        <p style='margin:0; font-size:24px; font-weight:800; color:{TB_GREEN_TEXT};'>{accepted_count}</p>
                    </div>
                    <div style='background:{TB_RED_FILL}; flex:1; padding:8px; border-radius:8px; text-align:center;'>
                        <p style='margin:0; font-size:9px; font-weight:800; color:{TB_RED_TEXT};'>DECLINED</p>
                        <p style='margin:0; font-size:24px; font-weight:800; color:{TB_RED_TEXT};'>{declined_count}</p>
                    </div>
                </div>
            </div>
        """, unsafe_allow_html=True)
    # 🌟 THE FIX: Force spacing before the Map
    st.markdown("<div style='margin-bottom: 25px;'></div>", unsafe_allow_html=True)
    
    # 🌟 Halt execution HERE, right after the cards render!
    if not is_initialized:
        st.info(f"No {pod_name} tasks initialized. Click '🚀 Initialize Data' at the top right.")
        return
        
    if not cls and not pod_ghosts:
        st.info(f"No active tasks pending in the {pod_name} region.")
        return

    m = folium.Map(location=cls[0]['center'], zoom_start=6, tiles="cartodbpositron")
    for c in ready: folium.CircleMarker(c['center'], radius=8, color=TB_GREEN, fill=True, opacity=0.8).add_to(m)
    for c in digital_ready: folium.CircleMarker(c['center'], radius=8, color="#0f766e", fill=True, opacity=0.8).add_to(m)
    for c in sent: folium.CircleMarker(c['center'], radius=8, color="#3b82f6", fill=True, opacity=0.8).add_to(m)
    for c in review: folium.CircleMarker(c['center'], radius=8, color="#ef4444", fill=True, opacity=0.8).add_to(m)
    st_folium(m, height=400, use_container_width=True, key=f"map_{pod_name}")
    
    st.markdown("""
<div style="display: flex; justify-content: center; flex-wrap: wrap; gap: 20px; background: #ffffff; padding: 12px; border-radius: 12px; border: 1px solid #cbd5e1; margin-top: -10px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.05);">
    <div style="font-size: 11px; font-weight: 800; color: #64748b; text-transform: uppercase; align-self: center; margin-right: 10px;">Route Key:</div>
    <div style="font-size: 13px; cursor: help;" title="Route is within distance limits (<60mi) and standard rate (<$25/stop).">🟢 Ready</div>
    <div style="font-size: 13px; cursor: help;" title="Route is frozen and requires manual authorization before sending.">🔒 Action Required</div>
    <div style="font-size: 13px; cursor: help;" title="The calculated price per stop is $25.00 or higher.">💰 High Rate</div>
    <div style="font-size: 13px; cursor: help;" title="The closest contractor is more than 60 miles away.">📡 Long Distance</div>
    <div style="font-size: 13px; cursor: help;" title="Priority: Contains escalated tasks.">⭐ Escalated</div>
    <div style="font-size: 13px; cursor: help;" title="Digital Offline: Screen reported offline.">📵 Offline</div>
    <div style="font-size: 13px; cursor: help;" title="Digital Ins/Rem: Installation or removal of digital screen.">🔧 Ins/Rem</div>
    <div style="font-size: 13px; cursor: help;" title="Digital Service: Standard digital maintenance.">⚙️ Service</div>
    <div style="font-size: 13px; cursor: help;" title="Route was flagged for review (e.g., low density).">🔴 Flagged</div>
</div>
""", unsafe_allow_html=True)

    st.markdown("---")

    col_left, col_right = st.columns([4.5, 5.5])

    with col_left:
        st.markdown(f"<div style='font-size: 1.5rem; font-weight: 800; color: {TB_PURPLE}; text-align: center;'>🚀 Dispatch</div>", unsafe_allow_html=True)
        t_ready, t_flagged, t_fn, t_digital = st.tabs(["📥 Ready", "⚠️ Flagged", "🌐 Field Nation", "🔌 Digital"])

        with t_ready:
            if not ready: st.info("No tasks ready for dispatch.")
            else:
                sorted_ready = group_and_sort_by_proximity(ready)
                current_state = None
                for i, c in enumerate(sorted_ready):
                    # 🌟 Insert State Header
                    if c['state'] != current_state:
                        current_state = c['state']
                        st.markdown(f"<div style='font-size: 12px; font-weight: 800; color: #94a3b8; margin-top: 15px; margin-bottom: 5px; border-bottom: 1px solid #e2e8f0; padding-bottom: 2px; text-transform: uppercase; letter-spacing: 1px;'>📍 {current_state}</div>", unsafe_allow_html=True)
                        
                    badges = ""
                    if not ic_df.empty:
                        lat_col = next((col for col in ic_df.columns if str(col).strip().lower() == 'lat'), 'Lat')
                        lng_col = next((col for col in ic_df.columns if str(col).strip().lower() == 'lng'), 'Lng')
                        loc_col = next((col for col in ic_df.columns if str(col).strip().lower() == 'location'), 'Location')
                        if lat_col in ic_df.columns and lng_col in ic_df.columns:
                            v_ics = ic_df[~ic_df.astype(str).apply(lambda x: x.str.contains('Field Agent', case=False, na=False).any(), axis=1)].dropna(subset=[lat_col, lng_col]).copy()
                            if not v_ics.empty:
                                v_ics['d'] = v_ics.apply(lambda x: haversine(c['center'][0], c['center'][1], x[lat_col], x[lng_col]), axis=1)
                                closest_ic = v_ics.sort_values('d').iloc[0]
                                _, hrs, _ = get_gmaps(closest_ic[loc_col], [t['full'] for t in c['data'][:25]])
                                est_pay = max(c['stops'] * 18.0, hrs * 25.0)
                                est_rate = est_pay / c['stops'] if c['stops'] > 0 else 0
                                if est_rate >= 25.0: badges += " 💰"
                                if closest_ic['d'] > 60: badges += " 📡"
                                if est_rate >= 25.0 or closest_ic['d'] > 60: badges = " 🔒" + badges

                    esc_pill = f"  [ ⭐ {c.get('esc_count', 0)} ]" if c.get('esc_count', 0) > 0 else ""
                    inst_pill = f"  [ 🛠️ {c.get('inst_count', 0)} Installs ]" if c.get('inst_count', 0) > 0 else "" 
                    remov_pill = f"  [ 🗑️ {c.get('remov_count', 0)} Removal ]" if c.get('remov_count', 0) > 0 else ""
                    
                    with st.expander(f"{badges} 🟢 {c['city']}, {c['state']} | {c['stops']} Stops{inst_pill}{remov_pill}{esc_pill}"):
                        render_dispatch(i, c, pod_name)
                    
        with t_flagged:
            if not review: st.info("No flagged tasks requiring review.")
            else:
                sorted_review = group_and_sort_by_proximity(review)
                current_state = None
                for i, c in enumerate(sorted_review):
                    if c['state'] != current_state:
                        current_state = c['state']
                        st.markdown(f"<div style='font-size: 12px; font-weight: 800; color: #94a3b8; margin-top: 15px; margin-bottom: 5px; border-bottom: 1px solid #e2e8f0; padding-bottom: 2px; text-transform: uppercase; letter-spacing: 1px;'>📍 {current_state}</div>", unsafe_allow_html=True)
                    
                    esc_pill = f"  [ ⭐ {c.get('esc_count', 0)} ]" if c.get('esc_count', 0) > 0 else ""
                    inst_pill = f"  [ 🛠️ {c.get('inst_count', 0)} Installs ]" if c.get('inst_count', 0) > 0 else ""
                    remov_pill = f"  [ 🗑️ {c.get('remov_count', 0)} Removal ]" if c.get('remov_count', 0) > 0 else ""
                    
                    with st.expander(f"🔒 🔴 {c['city']}, {c['state']} | {c['stops']} Stops{inst_pill}{remov_pill}{esc_pill}"):
                        render_dispatch(i+1000, c, pod_name)

        with t_fn:
            if not field_nation: st.info("No routes currently moved to Field Nation.")
            else:
                sorted_fn = group_and_sort_by_proximity(field_nation)
                current_state = None
                for i, c in enumerate(sorted_fn):
                    if c['state'] != current_state:
                        current_state = c['state']
                        st.markdown(f"<div style='font-size: 12px; font-weight: 800; color: #94a3b8; margin-top: 15px; margin-bottom: 5px; border-bottom: 1px solid #e2e8f0; padding-bottom: 2px; text-transform: uppercase; letter-spacing: 1px;'>📍 {current_state}</div>", unsafe_allow_html=True)
                    
                    esc_pill = f"  [ ⭐ {c.get('esc_count', 0)} ]" if c.get('esc_count', 0) > 0 else ""
                    digi_pill = " 🔌" if c.get('is_digital') else ""
                    inst_pill = f"  [ 🛠️ {c.get('inst_count', 0)} Installs ]" if c.get('inst_count', 0) > 0 else ""
                    remov_pill = f"  [ 🗑️ {c.get('remov_count', 0)} Removal ]" if c.get('remov_count', 0) > 0 else ""
                    
                    with st.expander(f"🌐 FN:{digi_pill} {c['city']}, {c['state']} | {c['stops']} Stops{inst_pill}{remov_pill}{esc_pill}"):
                        render_dispatch(i+5000, c, pod_name)
                    
        with t_digital:
            if not digital_ready: st.info("No digital service tasks pending.")
            else:
                sorted_digi = group_and_sort_by_proximity(digital_ready)
                current_state = None
                for i, c in enumerate(sorted_digi):
                    if c['state'] != current_state:
                        current_state = c['state']
                        st.markdown(f"<div style='font-size: 12px; font-weight: 800; color: #94a3b8; margin-top: 15px; margin-bottom: 5px; border-bottom: 1px solid #e2e8f0; padding-bottom: 2px; text-transform: uppercase; letter-spacing: 1px;'>📍 {current_state}</div>", unsafe_allow_html=True)
                    
                    with st.expander(f"🔌{c['city']}, {c['state']} | {c['stops']} Stops"):
                        render_dispatch(i+7000, c, pod_name)
                    
    with col_right:
        st.markdown(f"<div style='font-size: 1.5rem; font-weight: 800; color: {TB_GREEN}; margin-bottom: 5px; text-align: center;'>⏳ Awaiting Confirmation</div>", unsafe_allow_html=True)
        t_sent, t_acc, t_dec, t_fin = st.tabs(["✉️ Sent (Pending)", "✅ Accepted", "❌ Declined", "🏁 Finalized"])
        
        with t_sent:
            if not sent and not sent_ghosts: st.info("No pending routes sent.")
            
            with t_sent:
            if not sent and not sent_ghosts: st.info("No pending routes sent.")
            
            # 1. Render Live Sent Routes (Read-Only)
            for i, c in enumerate(sent):
                ic_name = c.get('contractor_name', 'Unknown')
                task_ids = [str(tid['id']).strip() for tid in c['data']]
                cluster_hash = hashlib.md5("".join(sorted(task_ids)).encode()).hexdigest()
                
                comp = c.get('comp', 0)
                due = c.get('due', 'N/A')
                tasks_cnt = len(c['data'])
                stops_cnt = c['stops']
                wo_display = c.get('wo', ic_name)
                
                exp_col, btn_col = st.columns([8.2, 1.8], vertical_alignment="center")
                with exp_col:
                    with st.expander(f"✉️ {wo_display} | {c['city']}, {c['state']} | ${comp} | {stops_cnt} Stops | {tasks_cnt} Tasks | Due: {due}"):
                        st.info("Route sent. Awaiting contractor response.")
                        u_locs = []
                        for tk in c['data']:
                            if tk['full'] not in u_locs: u_locs.append(tk['full'])
                        loc_html = "".join([f"<li>{l}</li>" for l in u_locs])
                        st.markdown(f"<div style='font-size:11px; color:#64748b; background:#f8fafc; padding:8px; border-radius:6px; margin-bottom:10px; border:1px solid #e2e8f0;'><b>Location Record:</b><ul style='margin-top:4px; margin-bottom:0; padding-left:20px;'>{loc_html}</ul></div>", unsafe_allow_html=True)
                with btn_col:
                    with st.popover("↩️ Re-Route", use_container_width=True):
                        st.markdown(f"<p style='font-size:13px; text-align:center;'>Re-route from <b>{ic_name}</b>?</p>", unsafe_allow_html=True)
                        st.button("🚨 Yes, Re-Route", key=f"rev_sent_live_{cluster_hash}_{pod_name}", type="primary", use_container_width=True, on_click=move_to_dispatch, kwargs={"cluster_hash": cluster_hash, "ic_name": ic_name, "pod_name": pod_name, "action_label": "Re-Routed", "check_onfleet": True, "cluster_data": c})
            
            # 2. 🌟 Render Ghost Sent Routes
            for i, g in enumerate(sent_ghosts):
                if g.get('hash') in live_hashes: continue # Skip if already rendered above
                
                g_ic_name = g.get('contractor_name', 'Unknown')
                ghost_hash = g.get('hash', f"ghost_sent_{i}")
                wo_display = g.get('wo', g_ic_name)
                comp, due = g.get('pay', 0), g.get('due', 'N/A')
                stops_cnt, tasks_cnt = g.get('stops', 0), g.get('tasks', 0)
                
                exp_col, btn_col = st.columns([8.2, 1.8], vertical_alignment="center")
                with exp_col:
                    with st.expander(f"✉️ {wo_display} | {g.get('city')}, {g.get('state')} | ${comp} | {stops_cnt} Stops | {tasks_cnt} Tasks | Due: {due}"):
                        st.info("Route sent. Awaiting contractor response.")
                        raw_locs = [s.strip() for s in g.get('locs', '').split('|') if s.strip()]
                        if len(raw_locs) >= 3: task_locs = raw_locs[1:-1]
                        else: task_locs = raw_locs
                        u_locs = []
                        for l in task_locs:
                            if l not in u_locs: u_locs.append(l)
                        if u_locs:
                            loc_html = "".join([f"<li>{l}</li>" for l in u_locs])
                            st.markdown(f"<div style='font-size:11px; color:#64748b; background:#f8fafc; padding:8px; border-radius:6px; margin-bottom:10px; border:1px solid #e2e8f0;'><b>Location Record:</b><ul style='margin-top:4px; margin-bottom:0; padding-left:20px;'>{loc_html}</ul></div>", unsafe_allow_html=True)
                with btn_col:
                    with st.popover("↩️ Re-Route", use_container_width=True):
                        st.markdown(f"<p style='font-size:13px; text-align:center;'>Re-route from <b>{g_ic_name}</b>?</p>", unsafe_allow_html=True)
                        st.button("🚨 Yes, Re-Route", key=f"rev_ghost_sent_{ghost_hash}", type="primary", use_container_width=True, on_click=move_to_dispatch, kwargs={"cluster_hash": ghost_hash, "ic_name": g_ic_name, "pod_name": pod_name, "action_label": "Re-Routed", "check_onfleet": True, "cluster_data": g})
                            
        with t_acc:
            if not accepted and not pod_ghosts: st.info("Waiting for portal acceptances...")
            for i, c in enumerate(accepted):
                ic_name = c.get('contractor_name', 'Unknown')
                task_ids = [str(tid['id']).strip() for tid in c['data']]
                cluster_hash = hashlib.md5("".join(sorted(task_ids)).encode()).hexdigest()
                
                comp = c.get('comp', 0)
                due = c.get('due', 'N/A')
                tasks_cnt = len(c['data'])
                stops_cnt = c['stops']
                
                exp_col, btn_col = st.columns([8.2, 1.8], vertical_alignment="center")
                with exp_col:
                    with st.expander(f"✅ {c.get('wo', ic_name)} | {c['city']}, {c['state']} | ${comp} | {stops_cnt} Stops | {tasks_cnt} Tasks | Due: {due}"):
                        st.success("Route accepted. Complete the checklist to finalize.")
                        
                        # 🌟 Mini data record (Inside the expander)
                        u_locs = []
                        for tk in c['data']:
                            if tk['full'] not in u_locs: u_locs.append(tk['full'])
                        loc_html = "".join([f"<li>{l}</li>" for l in u_locs])
                        st.markdown(f"<div style='font-size:11px; color:#64748b; background:#f8fafc; padding:8px; border-radius:6px; margin-bottom:10px; border:1px solid #e2e8f0;'><b>Location Record:</b><ul style='margin-top:4px; margin-bottom:0; padding-left:20px;'>{loc_html}</ul></div>", unsafe_allow_html=True)
                        
                        # 🌟 THE FIX: Instant Checklist Fragment
                        render_finalization_checklist(cluster_hash, pod_name, "chk")

                        st.divider()
                        
                        # 🌟 Route Details (Inside the expander)
                        render_dispatch(i+2000, c, pod_name, is_sent=True)
                with btn_col:
                    with st.popover("↩️ Revoke", use_container_width=True):
                        st.markdown(f"<p style='font-size:13px; text-align:center;'>Are you sure you want to remove this route from <b>{ic_name}</b>?</p>", unsafe_allow_html=True)
                        # 🌟 CALLBACK FIX
                        st.button("🚨 Yes, Remove", key=f"rev_acc_{cluster_hash}_{pod_name}", type="primary", use_container_width=True, on_click=move_to_dispatch, kwargs={"cluster_hash": cluster_hash, "ic_name": ic_name, "pod_name": pod_name, "cluster_data": c})

            for i, g in enumerate(pod_ghosts):
                g_ic_name = g.get('contractor_name', 'Unknown')
                ghost_hash = g.get('hash', f"ghost_{i}")
                
                comp = g.get('pay', 0)
                due = g.get('due', 'N/A')
                stops_cnt = g.get('stops', 0)
                tasks_cnt = g.get('tasks', 0)
                
                exp_col, btn_col = st.columns([8.2, 1.8], vertical_alignment="center")
                with exp_col:
                    with st.expander(f"✅ {g.get('wo', g_ic_name)} | {g.get('city')}, {g.get('state')} | ${comp} | {stops_cnt} Stops | {tasks_cnt} Tasks | Due: {due}"):
                        st.success("Accepted and synced with OnFleet. Complete the checklist to finalize.")
                        
                        # 🌟 THE FIX: Inject the mini Location Record for Ghost Routes (Excluding Contractor Home)
                        raw_locs = [s.strip() for s in g.get('locs', '').split('|') if s.strip()]
                        
                        # Strip the first and last items (which are always the contractor's home address)
                        if len(raw_locs) >= 3:
                            task_locs = raw_locs[1:-1]
                        else:
                            task_locs = raw_locs # Fallback just in case
                            
                        u_locs = []
                        for l in task_locs:
                            if l not in u_locs: u_locs.append(l)
                        
                        if u_locs:
                            loc_html = "".join([f"<li>{l}</li>" for l in u_locs])
                            st.markdown(f"<div style='font-size:11px; color:#64748b; background:#f8fafc; padding:8px; border-radius:6px; margin-bottom:10px; border:1px solid #e2e8f0;'><b>Location Record:</b><ul style='margin-top:4px; margin-bottom:0; padding-left:20px;'>{loc_html}</ul></div>", unsafe_allow_html=True)
                        
                        # 🌟 THE FIX: Instant Checklist Fragment
                        render_finalization_checklist(ghost_hash, pod_name, "g_chk")
                with btn_col:
                    with st.popover("↩️ Revoke", use_container_width=True):
                        st.markdown(f"<p style='font-size:13px; text-align:center;'>Are you sure you want to remove this route from <b>{g_ic_name}</b>?</p>", unsafe_allow_html=True)
                        # 🌟 CALLBACK FIX
                        st.button("🚨 Yes, Remove", key=f"rev_ghost_{ghost_hash}_{i}", type="primary", use_container_width=True, on_click=move_to_dispatch, kwargs={"cluster_hash": ghost_hash, "ic_name": g_ic_name, "pod_name": pod_name, "action_label": "Ghost Archived", "check_onfleet": True, "cluster_data": g})
                    
        with t_dec:
            if not declined: st.info("No declined routes.")
            for i, c in enumerate(declined):
                ic_name = c.get('contractor_name', 'Unknown')
                task_ids = [str(tid['id']).strip() for tid in c['data']]
                cluster_hash = hashlib.md5("".join(sorted(task_ids)).encode()).hexdigest()
                exp_col, btn_col = st.columns([8.2, 1.8], vertical_alignment="center")
                with exp_col:
                    with st.expander(f"❌ {ic_name} | {c['city']}, {c['state']}"):
                        st.error("Route declined. Re-route to a new IC.")
                        render_dispatch(i+3000, c, pod_name, is_declined=True)
                with btn_col:
                    with st.popover("↩️ Revoke", use_container_width=True):
                        st.markdown(f"<p style='font-size:13px; text-align:center;'>Are you sure you want to remove this route from <b>{ic_name}</b>?</p>", unsafe_allow_html=True)
                        # 🌟 CALLBACK FIX
                        st.button("🚨 Yes, Remove", key=f"rev_dec_{cluster_hash}_{pod_name}", type="primary", use_container_width=True, on_click=move_to_dispatch, kwargs={"cluster_hash": cluster_hash, "ic_name": ic_name, "pod_name": pod_name, "cluster_data": c})
                    
        with t_fin:
            if not finalized and not finalized_ghosts: st.info("No finalized routes.") 
            for i, c in enumerate(finalized):                 
                ic_name = c.get('contractor_name', 'Unknown')
                task_ids = [str(tid['id']).strip() for tid in c['data']]
                cluster_hash = hashlib.md5("".join(sorted(task_ids)).encode()).hexdigest()
                
                comp = c.get('comp', 0)
                due = c.get('due', 'N/A')
                tasks_cnt = len(c['data'])
                stops_cnt = c['stops']
                
                exp_col, btn_col = st.columns([8.2, 1.8], vertical_alignment="center")
                with exp_col:
                    with st.expander(f"🏁 {c.get('wo', ic_name)} | {c['city']}, {c['state']} | ${comp} | {stops_cnt} Stops | {tasks_cnt} Tasks | Due: {due}"):
                        # 🌟 NEW: Add mini data record for Live Finalized Routes
                        u_locs = []
                        for tk in c['data']:
                            if tk['full'] not in u_locs: u_locs.append(tk['full'])
                        loc_html = "".join([f"<li>{l}</li>" for l in u_locs])
                        st.markdown(f"<div style='font-size:11px; color:#64748b; background:#f8fafc; padding:8px; border-radius:6px; margin-bottom:10px; border:1px solid #e2e8f0;'><b>Location Record:</b><ul style='margin-top:4px; margin-bottom:0; padding-left:20px;'>{loc_html}</ul></div>", unsafe_allow_html=True)
                        
                        render_dispatch(i+4000, c, pod_name, is_sent=True)
                with btn_col:
                    with st.popover("↩️ Re-Route", use_container_width=True):
                        st.markdown(f"<p style='font-size:13px; text-align:center;'>Re-route from <b>{ic_name}</b>?</p>", unsafe_allow_html=True)
                        # 🌟 CALLBACK FIX
                        st.button("🚨 Yes, Re-Route", key=f"quick_reroute_{cluster_hash}_{pod_name}", type="primary", use_container_width=True, on_click=move_to_dispatch, kwargs={"cluster_hash": cluster_hash, "ic_name": ic_name, "pod_name": pod_name, "action_label": "Re-Routed", "check_onfleet": True, "cluster_data": c})
                        
            # 🌟 THE FIX: Render Ghost Routes that were just finalized
            for i, g in enumerate(finalized_ghosts):
                g_ic_name = g.get('contractor_name', 'Unknown')
                ghost_hash = g.get('hash', f"ghost_fin_{i}")
                wo_display = g.get('wo', g_ic_name)
                
                comp = g.get('pay', 0)
                due = g.get('due', 'N/A')
                stops_cnt = g.get('stops', 0)
                tasks_cnt = g.get('tasks', 0)
                
                exp_col, btn_col = st.columns([8.2, 1.8], vertical_alignment="center")
                with exp_col:
                    with st.expander(f"🏁 {wo_display} | {g.get('city')}, {g.get('state')} | ${comp} | {stops_cnt} Stops | {tasks_cnt} Tasks | Due: {due}"):
                        st.success("Route Finalized and Archived.")
                        # Render the mini location record for proof
                        raw_locs = [s.strip() for s in g.get('locs', '').split('|') if s.strip()]
                        if len(raw_locs) >= 3:
                            task_locs = raw_locs[1:-1]
                        else:
                            task_locs = raw_locs
                        u_locs = []
                        for l in task_locs:
                            if l not in u_locs: u_locs.append(l)
                        if u_locs:
                            loc_html = "".join([f"<li>{l}</li>" for l in u_locs])
                            st.markdown(f"<div style='font-size:11px; color:#64748b; background:#f8fafc; padding:8px; border-radius:6px; margin-bottom:10px; border:1px solid #e2e8f0;'><b>Location Record:</b><ul style='margin-top:4px; margin-bottom:0; padding-left:20px;'>{loc_html}</ul></div>", unsafe_allow_html=True)
                with btn_col:
                    st.button("🔒 Finalized", key=f"fin_locked_{ghost_hash}", disabled=True, use_container_width=True)
    st.markdown("---")
# --- START ---
if "ic_df" not in st.session_state:
    try:
        url = f"{IC_SHEET_URL.split('/edit')[0]}/export?format=csv&gid=0"
        df = pd.read_csv(url)
        # 🌟 BULLETPROOF: Lowercase all headers the second the data is downloaded
        df.columns = [str(c).strip().lower() for c in df.columns]
        st.session_state.ic_df = df
    except: st.error("Database connection failed.")

# --- HEADER ROW (Title & Refresh Button) ---
# [1, 8, 2] ratio gives the refresh button enough room to stay flat
col_left_space, col_main_title, col_ref = st.columns([1, 8, 2])

with col_main_title:
    st.markdown("<h1 style='color: #633094;'>Terraboost Media: Dispatch Command Center</h1>", unsafe_allow_html=True)

with col_ref:
    st.markdown("<div class='refresh-btn-container' style='margin-top: 26px;'>", unsafe_allow_html=True)
    if st.button("🔄 Refresh", key="top_ref_btn"):
        st.cache_data.clear()
        st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)

# Updated Main Tabs
tabs = st.tabs(["Global", "Blue Pod", "Green Pod", "Orange Pod", "Purple Pod", "Red Pod", "Digital"])
# --- TAB 0: GLOBAL CONTROL ---
with tabs[0]:
    # Check if ANY pod is loaded to toggle button state
    has_global_data = any(f"clusters_{p}" in st.session_state for p in POD_CONFIGS.keys())
    
    # 🌟 NEW HEADER: Title Centered, Dynamic Button Top Right
    gh_col1, gh_col2, gh_col3 = st.columns([2, 6, 2])
    with gh_col2:
        st.markdown("<h2 style='color: #633094; text-align:center; margin-top: 0;'>🌍 Global Overview</h2>", unsafe_allow_html=True)
    with gh_col3:
        st.markdown("<div class='tab-action-btn'>", unsafe_allow_html=True)
        btn_label = "🚀 Sync Routes" if has_global_data else "🚀 Initialize All Pods"
        if st.button(btn_label, key="global_init_btn", use_container_width=True):
            st.session_state.sent_db, st.session_state.ghost_db = fetch_sent_records_from_sheet()
            st.session_state.trigger_pull = True
            st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("---")
    loading_placeholder = st.empty()
    cols = st.columns(len(POD_CONFIGS))
    pod_keys = list(POD_CONFIGS.keys())
    global_map = folium.Map(location=[39.8283, -98.5795], zoom_start=4, tiles="cartodbpositron")
    current_sent_db, ghost_db = fetch_sent_records_from_sheet()

    for i, pod in enumerate(pod_keys):
        colors = {
            "Blue":   {"border": "#3b82f6", "bg": "#f0f7ff", "text": "#1e3a8a"},
            "Green":  {"border": "#22c55e", "bg": "#f0fdf4", "text": "#064e3b"},
            "Orange": {"border": "#f97316", "bg": "#fffaf5", "text": "#7c2d12"},
            "Purple": {"border": "#a855f7", "bg": "#faf5ff", "text": "#4c1d95"},
            "Red":    {"border": "#ef4444", "bg": "#fef2f2", "text": "#7f1d1d"}
        }.get(pod)
        
        with cols[i]:
            is_loading = st.session_state.get("current_loading_pod") == pod
            has_data = f"clusters_{pod}" in st.session_state
            
            if is_loading:
                card_content = f"<p class='loading-pulse' style='color:{colors['border']}; margin-top:25px;'>📡 SYNCING...</p>"
            elif has_data:
                pod_cls = st.session_state[f"clusters_{pod}"]
                total_routes = len(pod_cls)
                total_tasks = sum(len(c['data']) for c in pod_cls)
                total_stops = sum(c['stops'] for c in pod_cls)
                
                # 🌟 THE FIX: Initialize all required lists for the Global summary
                sent, accepted, declined, field_nation, ready, review, finalized = [], [], [], [], [], [], []
                
                for c in pod_cls:
                    task_ids = [str(t['id']).strip() for t in c['data']]
                    cluster_hash = hashlib.md5("".join(sorted(task_ids)).encode()).hexdigest()
                    sheet_match = current_sent_db.get(next((tid for tid in task_ids if tid in current_sent_db), None))
                    route_state = st.session_state.get(f"route_state_{cluster_hash}")
                    is_reverted = st.session_state.get(f"reverted_{cluster_hash}", False)
                    
                    # --- PRIORITY: LIVE DATABASE OVERRIDES LOCAL STATE ---
                    if sheet_match and not is_reverted:
                        raw_status = str(sheet_match.get('status', '')).lower()
                        if raw_status == 'field_nation':
                            field_nation.append(c)
                        elif raw_status == 'declined':
                            declined.append(c)
                        elif raw_status == 'accepted':
                            accepted.append(c)
                        elif raw_status == 'finalized': 
                            finalized.append(c)
                        else:
                            sent.append(c)
                    # 🌟 Handle Local Session State
                    elif route_state == "email_sent" and not is_reverted:
                        sent.append(c)
                    elif route_state == "field_nation" and not is_reverted: 
                        field_nation.append(c)
                    elif route_state == "link_generated" and not is_reverted:
                        orig = st.session_state.get(f"orig_status_{cluster_hash}")
                        if orig == "declined":
                            declined.append(c)
                        else:
                            ready.append(c)
                    else:
                        if c.get('status') == 'Ready': 
                            ready.append(c)
                        else: 
                            review.append(c)
                
                pod_ghosts = ghost_db.get(pod, [])
                total_accepted = len(accepted) + len(pod_ghosts)
                true_sent_count = len(sent) + len(field_nation) + total_accepted + len(declined)
                visual_total_routes = len(pod_cls) + len(pod_ghosts)
                
                card_content = f"""
<p style='margin: 10px 0 0 0; font-size: 26px; font-weight: 800; color: {colors['text']};'>{true_sent_count} / {visual_total_routes}</p>
<p style='margin: -5px 0 0 0; font-size: 11px; font-weight: 700; color: {colors['text']}; opacity: 0.6; text-transform: uppercase;'>Routes Sent</p>
<p style='margin: 2px 0 8px 0; font-size: 9px; font-weight: 700; color: {colors['text']}; opacity: 0.5;'>{total_accepted} ACCEPTED | {len(declined)} DECLINED</p>
<div style='display: flex; justify-content: space-around; border-top: 1px solid rgba(0,0,0,0.08); padding-top: 10px;'>
<div><p style='margin:0; font-size:9px; color: {colors['text']}; opacity: 0.8; font-weight: 800;'>TASKS</p><b style='color: {colors['text']};'>{total_tasks}</b></div>
<div style='border-left: 1px solid rgba(0,0,0,0.08); height: 20px;'></div>
<div><p style='margin:0; font-size:9px; color: {colors['text']}; opacity: 0.8; font-weight: 800;'>STOPS</p><b style='color: {colors['text']};'>{total_stops}</b></div>
</div>
"""
                for c in pod_cls: folium.CircleMarker(c['center'], radius=5, color=colors['border'], fill=True, fill_opacity=0.7).add_to(global_map)
            else:
                card_content = f"<p style='color: {colors['text']}; opacity: 0.3; font-weight: 800; margin-top: 30px;'>OFFLINE</p>"

            st.markdown(f"""
<div class="pod-card-pill" style="border: 2px solid {colors['border']}; border-radius: 30px; padding: 20px 10px; background-color: {colors['bg']}; text-align: center; height: 190px; box-shadow: 0 4px 10px rgba(0,0,0,0.03); display: flex; flex-direction: column; justify-content: center;">
<div style="margin: 0; color: {colors['text']}; font-weight: 800; font-size: 1.2rem;">{pod} Pod</div>
{card_content}
</div>
""", unsafe_allow_html=True)
            
    if st.session_state.get("trigger_pull"):
        st.session_state.sent_db, st.session_state.ghost_db = fetch_sent_records_from_sheet()
        p_bar = loading_placeholder.progress(0, text="🎬 Initializing Operational Data...")
        for idx, p in enumerate(pod_keys):
            st.session_state.current_loading_pod = p 
            process_pod(p, master_bar=p_bar, pod_idx=idx, total_pods=len(pod_keys))
        st.session_state.current_loading_pod = None
        st.session_state.trigger_pull = False
        st.rerun()

    # 🌟 THE FIX: Inject the blue prompt right above the map if no data exists
    if not has_global_data:
        st.info("No operational data initialized. Click '🚀 Initialize All Pods' at the top right to fetch tasks across all pods.")

    st.markdown("<br> 🗺️ Master Route Map", unsafe_allow_html=True)
    st_folium(global_map, height=500, use_container_width=True, key="global_master_map")

# --- INDIVIDUAL POD TABS ---
# 🌟 FIX: Using 2 instead of 1 to account for the new Digital Pool tab!
for i, pod in enumerate(["Blue", "Green", "Orange", "Purple", "Red"], 1):
    with tabs[i]: run_pod_tab(pod)

# --- TAB 6: DIGITAL POOL ---
with tabs[6]:
    # 1. 📊 GRAB DATA & INITIALIZE
    global_digital = st.session_state.get('global_digital_clusters', [])
    
    # 🌟 THE FIX: Omni-Ghost Sorter for Digital
    sent_db, ghost_db = fetch_sent_records_from_sheet()
    digital_ghosts_list = ghost_db.get("Global_Digital", [])
    
    pod_ghosts, finalized_ghosts, sent_ghosts = [], [], []
    
    for g in digital_ghosts_list:
        g_stat = g.get("status", "")
        local_override = st.session_state.get(f"route_state_{g.get('hash')}")
        if local_override == "finalized" or g_stat == "finalized": finalized_ghosts.append(g)
        elif g_stat == "sent": sent_ghosts.append(g)
        else: pod_ghosts.append(g)
    
    # --- 🚦 TRAFFIC COP: BUCKET SORTING (Pulls WO from Sheet) ---
    d_ready, d_flagged, d_fn, d_sent, d_acc, d_dec, d_fin = [], [], [], [], [], [], []
    live_hashes = set() # 🌟 Track live routes so we don't duplicate them!
    
    for c in global_digital:
        task_ids = [str(t['id']).strip() for t in c['data']]
        cluster_hash = hashlib.md5("".join(sorted(task_ids)).encode()).hexdigest()
        live_hashes.add(cluster_hash) # Save hash
        
        route_state = st.session_state.get(f"route_state_{cluster_hash}")
        is_reverted = st.session_state.get(f"reverted_{cluster_hash}", False)
        
        # 🌟 Fetch Local Memory
        local_ts = st.session_state.get(f"sent_ts_{cluster_hash}", "")
        local_contractor = st.session_state.get(f"contractor_{cluster_hash}", "Unknown")
        local_wo = st.session_state.get(f"wo_{cluster_hash}", local_contractor)
        
        # Match live sheet data to get the Contractor Name and WO
        sheet_match = sent_db.get(next((tid for tid in task_ids if tid in sent_db), None))
        if sheet_match and not is_reverted:
            c['contractor_name'] = sheet_match.get('name', 'Unknown')
            c['wo'] = sheet_match.get('wo', c['contractor_name'])
            c['route_ts'] = sheet_match.get('time', '') or local_ts
            c['comp'] = sheet_match.get('comp', 0)    # 🌟 NEW
            c['due'] = sheet_match.get('due', 'N/A')  # 🌟 NEW
            db_stat = sheet_match.get('status', 'sent').lower()
        else:
            # 🌟 Apply Fallbacks Instantly
            c['contractor_name'] = local_contractor
            c['wo'] = local_wo
            c['route_ts'] = local_ts
            db_stat = c.get('db_status', 'ready').lower()

        # 🌟 LOGIC GATE: Every .append() target MUST start with 'd_'
        if route_state == 'finalized': d_fin.append(c) # 🌟 THE FIX: Local Finalize Override
        elif db_stat in ['sent', 'email_sent'] and not is_reverted: d_sent.append(c) 
        elif db_stat == 'accepted' and not is_reverted: d_acc.append(c) 
        elif db_stat == 'declined' and not is_reverted: d_dec.append(c) 
        elif db_stat == 'finalized' and not is_reverted: d_fin.append(c)
        elif db_stat == 'field_nation' and not is_reverted: d_fn.append(c) 
        elif route_state == 'email_sent' and not is_reverted: d_sent.append(c) 
        elif route_state == 'field_nation' and not is_reverted: d_fn.append(c) 
        # 👇 Added this safeguard back in just in case!
        elif route_state == 'link_generated' and not is_reverted:
            orig = st.session_state.get(f"orig_status_{cluster_hash}")
            if orig == "declined": d_dec.append(c)
            else: d_ready.append(c)
        else:
            if c.get('status') == 'Ready': d_ready.append(c) 
            else: d_flagged.append(c)
        if st.session_state.get(f"route_state_{g.get('hash')}") == "finalized" or g.get("status") == "finalized":
            finalized_ghosts.append(g)
        else:
            pod_ghosts.append(g)
        if st.session_state.get(f"route_state_{g.get('hash')}") == "finalized" or g.get("status") == "finalized":
            finalized_ghosts.append(g)
        else:
            pod_ghosts.append(g)
    
    # --- 🚦 TRAFFIC COP: BUCKET SORTING (Pulls WO from Sheet) ---
    d_ready, d_flagged, d_fn, d_sent, d_acc, d_dec, d_fin = [], [], [], [], [], [], []
    
    for c in global_digital:
        task_ids = [str(t['id']).strip() for t in c['data']]
        cluster_hash = hashlib.md5("".join(sorted(task_ids)).encode()).hexdigest()
        route_state = st.session_state.get(f"route_state_{cluster_hash}")
        is_reverted = st.session_state.get(f"reverted_{cluster_hash}", False)
        
        # 🌟 Fetch Local Memory
        local_ts = st.session_state.get(f"sent_ts_{cluster_hash}", "")
        local_contractor = st.session_state.get(f"contractor_{cluster_hash}", "Unknown")
        local_wo = st.session_state.get(f"wo_{cluster_hash}", local_contractor)
        
        # Match live sheet data to get the Contractor Name and WO
        sheet_match = sent_db.get(next((tid for tid in task_ids if tid in sent_db), None))
        if sheet_match and not is_reverted:
            c['contractor_name'] = sheet_match.get('name', 'Unknown')
            c['wo'] = sheet_match.get('wo', c['contractor_name'])
            c['route_ts'] = sheet_match.get('time', '') or local_ts
            c['comp'] = sheet_match.get('comp', 0)    # 🌟 NEW
            c['due'] = sheet_match.get('due', 'N/A')  # 🌟 NEW
            db_stat = sheet_match.get('status', 'sent').lower()
        else:
            # 🌟 Apply Fallbacks Instantly
            c['contractor_name'] = local_contractor
            c['wo'] = local_wo
            c['route_ts'] = local_ts
            db_stat = c.get('db_status', 'ready').lower()

        # 🌟 LOGIC GATE: Every .append() target MUST start with 'd_'
        if route_state == 'finalized': d_fin.append(c) # 🌟 THE FIX: Local Finalize Override
        elif db_stat in ['sent', 'email_sent'] and not is_reverted: d_sent.append(c) 
        elif db_stat == 'accepted' and not is_reverted: d_acc.append(c) 
        elif db_stat == 'declined' and not is_reverted: d_dec.append(c) 
        elif db_stat == 'finalized' and not is_reverted: d_fin.append(c)
        elif db_stat == 'field_nation' and not is_reverted: d_fn.append(c) 
        elif route_state == 'email_sent' and not is_reverted: d_sent.append(c) 
        elif route_state == 'field_nation' and not is_reverted: d_fn.append(c) 
        # 👇 Added this safeguard back in just in case!
        elif route_state == 'link_generated' and not is_reverted:
            orig = st.session_state.get(f"orig_status_{cluster_hash}")
            if orig == "declined": d_dec.append(c)
            else: d_ready.append(c)
        else:
            if c.get('status') == 'Ready': d_ready.append(c) 
            else: d_flagged.append(c)
                
    # Supercard Counts
    pool_ready = len(d_ready)
    pool_flagged = len(d_flagged)
    pool_total_sent = len(d_sent) + len(d_acc) + len(pod_ghosts) + len(d_dec) + len(d_fn)
    
    # 🌟 THE FIX: Combine active Digital buckets (Excludes Accepted & Finalized)
    active_d_cls = d_ready + d_flagged + d_fn + d_sent + d_dec
    tasks_total = sum(len(c['data']) for c in active_d_cls)
    unique_stops_total = len(set(t['full'] for c in active_d_cls for t in c['data']))
    
    # 2. ⚡ DIGITAL HEADER & DYNAMIC BUTTON
    dh_col1, dh_col2, dh_col3 = st.columns([2, 6, 2])
    with dh_col2:
        st.markdown(f"<div style='text-align:center; padding-bottom:15px;'><h2 style='color:{TB_DIGITAL_TEXT}; margin:0;'>🔌 Digital Services Dashboard</h2></div>", unsafe_allow_html=True)
    with dh_col3:
        st.markdown("<div class='tab-action-btn'>", unsafe_allow_html=True)
        btn_label = "🚀 Sync Routes" if global_digital else "🚀 Initialize Data"
        if st.button(btn_label, key="digital_init_btn", use_container_width=True):
            d_bar = st.progress(0, text="🎬 Initializing...")
            process_digital_pool(master_bar=d_bar)
            st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)

    # 3. 🃏 SUPERCARDS
    dc1, dc2, dc3 = st.columns([1, 1, 1])
    with dc1:
        st.markdown(f"<div class='dashboard-supercard' style='background:#ffffff; border:1px solid #cbd5e1; border-radius:12px; padding:12px; height: 110px;'><p style='margin:0 0 8px 0; font-size:10px; font-weight:800; color:#64748b; text-transform:uppercase; text-align:center;'>Status</p><div style='display:flex; justify-content:space-around; gap:8px;'><div style='background:{TB_GREEN_FILL}; flex:1; padding:8px; border-radius:8px; text-align:center;'><p style='margin:0; font-size:8px; font-weight:800; color:{TB_GREEN_TEXT};'>READY</p><p style='margin:0; font-size:22px; font-weight:800;'>{pool_ready}</p></div><div style='background:{TB_RED_FILL}; flex:1; padding:8px; border-radius:8px; text-align:center;'><p style='margin:0; font-size:8px; font-weight:800; color:{TB_RED_TEXT};'>FLAGGED</p><p style='margin:0; font-size:22px; font-weight:800;'>{pool_flagged}</p></div></div></div>", unsafe_allow_html=True)
    with dc2:
        # 🌟 UPDATED: Uses tasks_total instead of len(pool)
        st.markdown(f"<div class='dashboard-supercard' style='background:#ffffff; border:1px solid #cbd5e1; border-radius:12px; padding:12px; height: 110px;'><p style='margin:0 0 8px 0; font-size:10px; font-weight:800; color:#64748b; text-transform:uppercase; text-align:center;'>Workload</p><div style='display:flex; justify-content:space-around; gap:8px;'><div style='background:{TB_STATIC_FILL}; flex:1; padding:8px; border-radius:8px; text-align:center;'><p style='margin:0; font-size:8px; font-weight:800; color:{TB_STATIC_TEXT};'>TASKS</p><p style='margin:0; font-size:22px; font-weight:800;'>{tasks_total}</p></div><div style='background:{TB_STATIC_FILL}; flex:1; padding:8px; border-radius:8px; text-align:center;'><p style='margin:0; font-size:8px; font-weight:800; color:{TB_STATIC_TEXT};'>STOPS</p><p style='margin:0; font-size:22px; font-weight:800;'>{unique_stops_total}</p></div></div></div>", unsafe_allow_html=True)
    with dc3:
        st.markdown(f"<div class='dashboard-supercard' style='background:#ffffff; border:1px solid #cbd5e1; border-radius:12px; padding:12px; height:110px;'><p style='margin:0 0 8px 0; font-size:10px; font-weight:800; color:#64748b; text-transform:uppercase; text-align:center;'>Sent: {pool_total_sent}</p><div style='display:flex; justify-content:space-around; gap:8px;'><div style='background:{TB_GREEN_FILL}; flex:1; padding:8px; border-radius:8px; text-align:center;'><p style='margin:0; font-size:8px; font-weight:800; color:{TB_GREEN_TEXT};'>ACCEPTED</p><p style='margin:0; font-size:22px; font-weight:800;'>{len(d_acc)}</p></div><div style='background:{TB_RED_FILL}; flex:1; padding:8px; border-radius:8px; text-align:center;'><p style='margin:0; font-size:8px; font-weight:800; color:{TB_RED_TEXT};'>DECLINED</p><p style='margin:0; font-size:22px; font-weight:800;'>{len(d_dec)}</p></div></div></div>", unsafe_allow_html=True)
    # 🌟 THE FIX: Force spacing after the cards
    st.markdown("<div style='margin-bottom: 25px;'></div>", unsafe_allow_html=True)
    
    if not global_digital:
        st.info("Click '🚀 Initialize Data' at the top right to fetch data.")
    else:
        # 4. 🗺️ MAP & LEGEND
        m_digi = folium.Map(location=global_digital[0]['center'], zoom_start=4, tiles="cartodbpositron")
        for c in global_digital: folium.CircleMarker(c['center'], radius=8, color="#0f766e", fill=True, opacity=0.8).add_to(m_digi)
        st_folium(m_digi, height=400, use_container_width=True, key="digital_pool_map")
        
        st.markdown("""
<div style="display: flex; justify-content: center; flex-wrap: wrap; gap: 20px; background: #ffffff; padding: 12px; border-radius: 12px; border: 1px solid #cbd5e1; margin-top: -10px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.05);">
    <div style="font-size: 11px; font-weight: 800; color: #64748b; text-transform: uppercase; align-self: center; margin-right: 10px;">Route Key:</div>
    <div style="font-size: 13px; cursor: help;" title="Route is within distance limits (<60mi) and standard rate (<$25/stop).">🟢 Ready</div>
    <div style="font-size: 13px; cursor: help;" title="Digital Offline: Screen reported offline.">📵 Offline</div>
    <div style="font-size: 13px; cursor: help;" title="Digital Ins/Rem: Installation or removal of digital screen.">🔧 Ins/Rem</div>
    <div style="font-size: 13px; cursor: help;" title="Digital Service: Standard digital maintenance.">⚙️ Service</div>
    <div style="font-size: 13px; cursor: help;" title="Route was flagged for review (e.g., low density).">🔴 Flagged</div>
</div>
""", unsafe_allow_html=True)

        # 5. 🚀 TWO-COLUMN DISPATCH (Parity with Pods)
        st.markdown("---")
        col_left, col_right = st.columns([4.5, 5.5])
        
        with col_left:
            st.markdown(f"<div style='font-size: 1.5rem; font-weight: 800; color: {TB_DIGITAL_TEXT}; text-align: center;'>🚀 Dispatch</div>", unsafe_allow_html=True)
            t_ready, t_flagged, t_fn = st.tabs(["📥 Ready", "⚠️ Flagged", "🌐 Field Nation"])
            
            with t_ready:
                if not d_ready: st.info("No digital tasks ready for dispatch.")
                else:
                    sorted_d_ready = group_and_sort_by_proximity(d_ready)
                    current_state = None
                    for i, c in enumerate(sorted_d_ready):
                        if c['state'] != current_state:
                            current_state = c['state']
                            st.markdown(f"<div style='font-size: 12px; font-weight: 800; color: #94a3b8; margin-top: 15px; margin-bottom: 5px; border-bottom: 1px solid #e2e8f0; padding-bottom: 2px; text-transform: uppercase; letter-spacing: 1px;'>📍 {current_state}</div>", unsafe_allow_html=True)
                        with st.expander(f"{get_digi_badges(c['data'])} {c['city']}, {c['state']} | {c['stops']} Stops"):
                            render_dispatch(i+8000, c, "Global_Digital")
                            
            with t_flagged:
                if not d_flagged: st.info("No flagged tasks requiring review.")
                else:
                    sorted_d_flagged = group_and_sort_by_proximity(d_flagged)
                    current_state = None
                    for i, c in enumerate(sorted_d_flagged):
                        if c['state'] != current_state:
                            current_state = c['state']
                            st.markdown(f"<div style='font-size: 12px; font-weight: 800; color: #94a3b8; margin-top: 15px; margin-bottom: 5px; border-bottom: 1px solid #e2e8f0; padding-bottom: 2px; text-transform: uppercase; letter-spacing: 1px;'>📍 {current_state}</div>", unsafe_allow_html=True)
                        with st.expander(f"🔴 {get_digi_badges(c['data'])} {c['city']}, {c['state']} | {c['stops']} Stops"):
                            render_dispatch(i+9000, c, "Global_Digital")
                            
            with t_fn:
                if not d_fn: st.info("No tasks in Field Nation.")
                else:
                    sorted_d_fn = group_and_sort_by_proximity(d_fn)
                    current_state = None
                    for i, c in enumerate(sorted_d_fn):
                        if c['state'] != current_state:
                            current_state = c['state']
                            st.markdown(f"<div style='font-size: 12px; font-weight: 800; color: #94a3b8; margin-top: 15px; margin-bottom: 5px; border-bottom: 1px solid #e2e8f0; padding-bottom: 2px; text-transform: uppercase; letter-spacing: 1px;'>📍 {current_state}</div>", unsafe_allow_html=True)
                        with st.expander(f"🌐 FN {get_digi_badges(c['data'])} {c['city']}, {c['state']} | {c['stops']} Stops"):
                            render_dispatch(i+9500, c, "Global_Digital")

        with col_right:
            st.markdown(f"<div style='font-size: 1.5rem; font-weight: 800; color: {TB_GREEN}; text-align: center;'>⏳ Awaiting Confirmation</div>", unsafe_allow_html=True)
            t_sent, t_acc, t_dec, t_fin = st.tabs(["✉️ Sent", "✅ Accepted", "❌ Declined", "🏁 Finalized"])
            
            with t_sent:
                if not d_sent and not sent_ghosts: st.info("No pending routes sent.")
                
                # 1. Render Live Sent Routes (Read-Only)
                for i, c in enumerate(d_sent):
                    task_ids = [str(t['id']).strip() for t in c['data']]
                    cluster_hash = hashlib.md5("".join(sorted(task_ids)).encode()).hexdigest()
                    ic_name = c.get('contractor_name', 'Unknown')
                    
                    comp = c.get('comp', 0)
                    due = c.get('due', 'N/A')
                    tasks_cnt = len(c['data'])
                    stops_cnt = c['stops']
                    wo_display = c.get('wo', ic_name)
                    
                    exp_col, btn_col = st.columns([8.2, 1.8], vertical_alignment="center")
                    with exp_col:
                        with st.expander(f"✉️ {wo_display} | {c['city']}, {c['state']} | ${comp} | {stops_cnt} Stops | {tasks_cnt} Tasks | Due: {due}"):
                            st.info("Route sent. Awaiting contractor response.")
                            u_locs = []
                            for tk in c['data']:
                                if tk['full'] not in u_locs: u_locs.append(tk['full'])
                            loc_html = "".join([f"<li>{l}</li>" for l in u_locs])
                            st.markdown(f"<div style='font-size:11px; color:#64748b; background:#f8fafc; padding:8px; border-radius:6px; margin-bottom:10px; border:1px solid #e2e8f0;'><b>Location Record:</b><ul style='margin-top:4px; margin-bottom:0; padding-left:20px;'>{loc_html}</ul></div>", unsafe_allow_html=True)
                    with btn_col:
                        with st.popover("↩️ Re-Route", use_container_width=True):
                            st.markdown(f"<p style='font-size:13px; text-align:center;'>Re-route from <b>{ic_name}</b>?</p>", unsafe_allow_html=True)
                            st.button("🚨 Yes, Re-Route", key=f"rev_d_sent_live_{cluster_hash}", type="primary", use_container_width=True, on_click=move_to_dispatch, kwargs={"cluster_hash": cluster_hash, "ic_name": ic_name, "pod_name": "Global_Digital", "action_label": "Re-Routed", "check_onfleet": True, "cluster_data": c})
                
                # 2. 🌟 Render Ghost Sent Routes
                for i, g in enumerate(sent_ghosts):
                    if g.get('hash') in live_hashes: continue # Skip if already rendered above
                    
                    g_ic_name = g.get('contractor_name', 'Unknown')
                    ghost_hash = g.get('hash', f"ghost_d_sent_{i}")
                    wo_display = g.get('wo', g_ic_name)
                    comp, due = g.get('pay', 0), g.get('due', 'N/A')
                    stops_cnt, tasks_cnt = g.get('stops', 0), g.get('tasks', 0)
                    
                    exp_col, btn_col = st.columns([8.2, 1.8], vertical_alignment="center")
                    with exp_col:
                        with st.expander(f"✉️ {wo_display} | {g.get('city')}, {g.get('state')} | ${comp} | {stops_cnt} Stops | {tasks_cnt} Tasks | Due: {due}"):
                            st.info("Route sent. Awaiting contractor response.")
                            raw_locs = [s.strip() for s in g.get('locs', '').split('|') if s.strip()]
                            if len(raw_locs) >= 3: task_locs = raw_locs[1:-1]
                            else: task_locs = raw_locs
                            u_locs = []
                            for l in task_locs:
                                if l not in u_locs: u_locs.append(l)
                            if u_locs:
                                loc_html = "".join([f"<li>{l}</li>" for l in u_locs])
                                st.markdown(f"<div style='font-size:11px; color:#64748b; background:#f8fafc; padding:8px; border-radius:6px; margin-bottom:10px; border:1px solid #e2e8f0;'><b>Location Record:</b><ul style='margin-top:4px; margin-bottom:0; padding-left:20px;'>{loc_html}</ul></div>", unsafe_allow_html=True)
                    with btn_col:
                        with st.popover("↩️ Re-Route", use_container_width=True):
                            st.markdown(f"<p style='font-size:13px; text-align:center;'>Re-route from <b>{g_ic_name}</b>?</p>", unsafe_allow_html=True)
                            st.button("🚨 Yes, Re-Route", key=f"rev_ghost_d_sent_{ghost_hash}_{i}", type="primary", use_container_width=True, on_click=move_to_dispatch, kwargs={"cluster_hash": ghost_hash, "ic_name": g_ic_name, "pod_name": "Global_Digital", "action_label": "Re-Routed", "check_onfleet": True, "cluster_data": g})
            
            with t_acc:
                if not d_acc and not pod_ghosts: st.info("Waiting for portal acceptances...")
                
                # 1. Render Live Accepted Routes
                for i, c in enumerate(d_acc):
                    task_ids = [str(t['id']).strip() for t in c['data']]
                    cluster_hash = hashlib.md5("".join(sorted(task_ids)).encode()).hexdigest()
                    ic_name = c.get('contractor_name', 'Unknown')
                    
                    comp = c.get('comp', 0)
                    due = c.get('due', 'N/A')
                    tasks_cnt = len(c['data'])
                    stops_cnt = c['stops']
                    
                    exp_col, btn_col = st.columns([8.2, 1.8], vertical_alignment="center")
                    with exp_col:
                        with st.expander(f"✅ {c.get('wo', ic_name)} | {c['city']}, {c['state']} | ${comp} | {stops_cnt} Stops | {tasks_cnt} Tasks | Due: {due}"):
                            st.success("Route accepted. Complete the checklist to finalize.")
                            
                            u_locs = []
                            for tk in c['data']:
                                if tk['full'] not in u_locs: u_locs.append(tk['full'])
                            loc_html = "".join([f"<li>{l}</li>" for l in u_locs])
                            st.markdown(f"<div style='font-size:11px; color:#64748b; background:#f8fafc; padding:8px; border-radius:6px; margin-bottom:10px; border:1px solid #e2e8f0;'><b>Location Record:</b><ul style='margin-top:4px; margin-bottom:0; padding-left:20px;'>{loc_html}</ul></div>", unsafe_allow_html=True)
                            
                            render_finalization_checklist(cluster_hash, "Global_Digital", "d_chk")
                            st.divider()
                            render_dispatch(i+11000, c, "Global_Digital", is_sent=True)
                    with btn_col:
                        with st.popover("↩️ Revoke", use_container_width=True):
                            st.markdown(f"<p style='font-size:13px; text-align:center;'>Are you sure you want to remove this route from <b>{ic_name}</b>?</p>", unsafe_allow_html=True)
                            st.button("🚨 Yes, Remove", key=f"rev_d_acc_{cluster_hash}", type="primary", use_container_width=True, on_click=move_to_dispatch, kwargs={"cluster_hash": cluster_hash, "ic_name": ic_name, "pod_name": "Global_Digital", "cluster_data": c})

                # 2. 🌟 Render Ghost Accepted Routes
                for i, g in enumerate(pod_ghosts):
                    g_ic_name = g.get('contractor_name', 'Unknown')
                    ghost_hash = g.get('hash', f"ghost_digi_{i}")
                    
                    comp = g.get('pay', 0)
                    due = g.get('due', 'N/A')
                    stops_cnt = g.get('stops', 0)
                    tasks_cnt = g.get('tasks', 0)
                    
                    exp_col, btn_col = st.columns([8.2, 1.8], vertical_alignment="center")
                    with exp_col:
                        with st.expander(f"✅ {g.get('wo', g_ic_name)} | {g.get('city')}, {g.get('state')} | ${comp} | {stops_cnt} Stops | {tasks_cnt} Tasks | Due: {due}"):
                            st.success("Accepted and synced with OnFleet. Complete the checklist to finalize.")
                            
                            raw_locs = [s.strip() for s in g.get('locs', '').split('|') if s.strip()]
                            if len(raw_locs) >= 3: task_locs = raw_locs[1:-1]
                            else: task_locs = raw_locs
                                
                            u_locs = []
                            for l in task_locs:
                                if l not in u_locs: u_locs.append(l)
                            
                            if u_locs:
                                loc_html = "".join([f"<li>{l}</li>" for l in u_locs])
                                st.markdown(f"<div style='font-size:11px; color:#64748b; background:#f8fafc; padding:8px; border-radius:6px; margin-bottom:10px; border:1px solid #e2e8f0;'><b>Location Record:</b><ul style='margin-top:4px; margin-bottom:0; padding-left:20px;'>{loc_html}</ul></div>", unsafe_allow_html=True)
                            
                            render_finalization_checklist(ghost_hash, "Global_Digital", "g_chk_d")
                    with btn_col:
                        with st.popover("↩️ Revoke", use_container_width=True):
                            st.markdown(f"<p style='font-size:13px; text-align:center;'>Are you sure you want to remove this route from <b>{g_ic_name}</b>?</p>", unsafe_allow_html=True)
                            st.button("🚨 Yes, Remove", key=f"rev_ghost_digi_{ghost_hash}_{i}", type="primary", use_container_width=True, on_click=move_to_dispatch, kwargs={"cluster_hash": ghost_hash, "ic_name": g_ic_name, "pod_name": "Global_Digital", "action_label": "Ghost Archived", "check_onfleet": True, "cluster_data": g})

                # 🌟 THE FIX: Render Digital Ghost Routes
                for i, g in enumerate(pod_ghosts):
                    g_ic_name = g.get('contractor_name', 'Unknown')
                    ghost_hash = g.get('hash', f"ghost_digi_{i}")
                    
                    comp = g.get('pay', 0)
                    due = g.get('due', 'N/A')
                    stops_cnt = g.get('stops', 0)
                    tasks_cnt = g.get('tasks', 0)
                    
                    exp_col, btn_col = st.columns([8.2, 1.8], vertical_alignment="center")
                    with exp_col:
                        with st.expander(f"✅ {g.get('wo', g_ic_name)} | {g.get('city')}, {g.get('state')} | ${comp} | {stops_cnt} Stops | {tasks_cnt} Tasks | Due: {due}"):
                            st.success("Accepted and synced with OnFleet. Complete the checklist to finalize.")
                            
                            raw_locs = [s.strip() for s in g.get('locs', '').split('|') if s.strip()]
                            if len(raw_locs) >= 3: task_locs = raw_locs[1:-1]
                            else: task_locs = raw_locs
                                
                            u_locs = []
                            for l in task_locs:
                                if l not in u_locs: u_locs.append(l)
                            
                            if u_locs:
                                loc_html = "".join([f"<li>{l}</li>" for l in u_locs])
                                st.markdown(f"<div style='font-size:11px; color:#64748b; background:#f8fafc; padding:8px; border-radius:6px; margin-bottom:10px; border:1px solid #e2e8f0;'><b>Location Record:</b><ul style='margin-top:4px; margin-bottom:0; padding-left:20px;'>{loc_html}</ul></div>", unsafe_allow_html=True)
                            
                            render_finalization_checklist(ghost_hash, "Global_Digital", "g_chk_d")
                    with btn_col:
                        with st.popover("↩️ Revoke", use_container_width=True):
                            st.markdown(f"<p style='font-size:13px; text-align:center;'>Are you sure you want to remove this route from <b>{g_ic_name}</b>?</p>", unsafe_allow_html=True)
                            st.button("🚨 Yes, Remove", key=f"rev_ghost_digi_{ghost_hash}_{i}", type="primary", use_container_width=True, on_click=move_to_dispatch, kwargs={"cluster_hash": ghost_hash, "ic_name": g_ic_name, "pod_name": "Global_Digital", "action_label": "Ghost Archived", "check_onfleet": True, "cluster_data": g})
                    cluster_hash = hashlib.md5("".join(sorted(task_ids)).encode()).hexdigest()
                    ic_name = c.get('contractor_name', 'Unknown')
                    
                    comp = c.get('comp', 0)
                    due = c.get('due', 'N/A')
                    tasks_cnt = len(c['data'])
                    stops_cnt = c['stops']
                    
                    exp_col, btn_col = st.columns([8.2, 1.8], vertical_alignment="center")
                    with exp_col:
                        with st.expander(f"✅ {c.get('wo', ic_name)} | {c['city']}, {c['state']} | ${comp} | {stops_cnt} Stops | {tasks_cnt} Tasks | Due: {due}"):
                            st.success("Route accepted. Complete the checklist to finalize.")
                            
                            # 🌟 THE FIX: Add mini data record (Live Tasks)
                            u_locs = []
                            for tk in c['data']:
                                if tk['full'] not in u_locs: u_locs.append(tk['full'])
                            loc_html = "".join([f"<li>{l}</li>" for l in u_locs])
                            st.markdown(f"<div style='font-size:11px; color:#64748b; background:#f8fafc; padding:8px; border-radius:6px; margin-bottom:10px; border:1px solid #e2e8f0;'><b>Location Record:</b><ul style='margin-top:4px; margin-bottom:0; padding-left:20px;'>{loc_html}</ul></div>", unsafe_allow_html=True)
                            
                            # 🌟 THE FIX: Instant Checklist Fragment
                            render_finalization_checklist(cluster_hash, "Global_Digital", "d_chk")

                            st.divider()
                            render_dispatch(i+11000, c, "Global_Digital", is_sent=True)
                    with btn_col:
                        with st.popover("↩️ Revoke", use_container_width=True):
                            st.markdown(f"<p style='font-size:13px; text-align:center;'>Are you sure you want to remove this route from <b>{ic_name}</b>?</p>", unsafe_allow_html=True)
                            # 🌟 CALLBACK FIX (No indented code underneath!)
                            st.button("🚨 Yes, Remove", key=f"rev_d_acc_{cluster_hash}", type="primary", use_container_width=True, on_click=move_to_dispatch, kwargs={"cluster_hash": cluster_hash, "ic_name": ic_name, "pod_name": "Global_Digital", "cluster_data": c})

            with t_dec:
                for i, c in enumerate(d_dec):
                    task_ids = [str(t['id']).strip() for t in c['data']]
                    cluster_hash = hashlib.md5("".join(sorted(task_ids)).encode()).hexdigest()
                    ic_name = c.get('contractor_name', 'Unknown')
                    exp_col, btn_col = st.columns([8.2, 1.8], vertical_alignment="center")
                    with exp_col:
                        with st.expander(f"❌ {c.get('wo', ic_name)} | {c['city']}, {c['state']}"):
                            render_dispatch(i+12000, c, "Global_Digital", is_declined=True)
                    with btn_col:
                        with st.popover("↩️ Revoke", use_container_width=True):
                            st.markdown(f"<p style='font-size:13px; text-align:center;'>Are you sure you want to remove this route from <b>{ic_name}</b>?</p>", unsafe_allow_html=True)
                            # 🌟 CALLBACK FIX (No indented code underneath!)
                            st.button("🚨 Yes, Remove", key=f"rev_d_dec_{cluster_hash}", type="primary", use_container_width=True, on_click=move_to_dispatch, kwargs={"cluster_hash": cluster_hash, "ic_name": ic_name, "pod_name": "Global_Digital", "cluster_data": c})
                    
            with t_fin:
                if not d_fin and not finalized_ghosts: st.info("No finalized digital routes.") 
                for i, c in enumerate(d_fin):                       
                    task_ids = [str(t['id']).strip() for t in c['data']]
                    cluster_hash = hashlib.md5("".join(sorted(task_ids)).encode()).hexdigest()
                    ic_name = c.get('contractor_name', 'Unknown')
                    
                    comp = c.get('comp', 0)
                    due = c.get('due', 'N/A')
                    tasks_cnt = len(c['data'])
                    stops_cnt = c['stops']
                    
                    exp_col, btn_col = st.columns([8.2, 1.8], vertical_alignment="center")
                    with exp_col:
                        with st.expander(f"🏁 {c.get('wo', ic_name)} | {c['city']}, {c['state']} | ${comp} | {stops_cnt} Stops | {tasks_cnt} Tasks | Due: {due}"):
                            u_locs = []
                            for tk in c['data']:
                                if tk['full'] not in u_locs: u_locs.append(tk['full'])
                            loc_html = "".join([f"<li>{l}</li>" for l in u_locs])
                            st.markdown(f"<div style='font-size:11px; color:#64748b; background:#f8fafc; padding:8px; border-radius:6px; margin-bottom:10px; border:1px solid #e2e8f0;'><b>Location Record:</b><ul style='margin-top:4px; margin-bottom:0; padding-left:20px;'>{loc_html}</ul></div>", unsafe_allow_html=True)
                            
                            render_dispatch(i+13000, c, "Global_Digital", is_sent=True)
                    with btn_col:
                        with st.popover("↩️ Re-Route", use_container_width=True):
                            st.markdown(f"<p style='font-size:13px; text-align:center;'>Re-route from <b>{ic_name}</b>?</p>", unsafe_allow_html=True)
                            st.button("🚨 Yes, Re-Route", key=f"rev_d_fin_{cluster_hash}", type="primary", use_container_width=True, on_click=move_to_dispatch, kwargs={"cluster_hash": cluster_hash, "ic_name": ic_name, "pod_name": "Global_Digital", "action_label": "Re-Routed", "check_onfleet": True, "cluster_data": c})
                            
                # 🌟 THE FIX: Render Finalized Digital Ghost Routes
                for i, g in enumerate(finalized_ghosts):
                    g_ic_name = g.get('contractor_name', 'Unknown')
                    ghost_hash = g.get('hash', f"ghost_fin_digi_{i}")
                    wo_display = g.get('wo', g_ic_name)
                    
                    comp = g.get('pay', 0)
                    due = g.get('due', 'N/A')
                    stops_cnt = g.get('stops', 0)
                    tasks_cnt = g.get('tasks', 0)
                    
                    exp_col, btn_col = st.columns([8.2, 1.8], vertical_alignment="center")
                    with exp_col:
                        with st.expander(f"🏁 {wo_display} | {g.get('city')}, {g.get('state')} | ${comp} | {stops_cnt} Stops | {tasks_cnt} Tasks | Due: {due}"):
                            st.success("Route Finalized and Archived.")
                            
                            raw_locs = [s.strip() for s in g.get('locs', '').split('|') if s.strip()]
                            if len(raw_locs) >= 3: task_locs = raw_locs[1:-1]
                            else: task_locs = raw_locs
                            
                            u_locs = []
                            for l in task_locs:
                                if l not in u_locs: u_locs.append(l)
                            if u_locs:
                                loc_html = "".join([f"<li>{l}</li>" for l in u_locs])
                                st.markdown(f"<div style='font-size:11px; color:#64748b; background:#f8fafc; padding:8px; border-radius:6px; margin-bottom:10px; border:1px solid #e2e8f0;'><b>Location Record:</b><ul style='margin-top:4px; margin-bottom:0; padding-left:20px;'>{loc_html}</ul></div>", unsafe_allow_html=True)
                    with btn_col:
                        st.button("🔒 Finalized", key=f"fin_locked_digi_{ghost_hash}", disabled=True, use_container_width=True)
                        
# --- FINAL FOOTER (End of File) ---
st.markdown("---")
st.markdown(
    """
    <div style="text-align: center; color: #94a3b8; font-size: 12px; padding: 20px;">
        Tactical Workspace Master • 2026 Digital Logistics Interface • <b>v2.4.0</b><br>
        <i>All digital and static route data is synced in real-time.</i>
    </div>
    """, 
    unsafe_allow_html=True
)
