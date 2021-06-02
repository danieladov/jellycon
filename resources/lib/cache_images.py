# coding=utf-8
# Gnu General Public License - see LICENSE.TXT
from __future__ import division, absolute_import, print_function, unicode_literals

from six.moves.urllib.parse import unquote
import requests
import base64
import sys
import threading
import time

import xbmcgui
import xbmcplugin
import xbmc
import xbmcaddon

from .downloadutils import DownloadUtils
from .loghandler import LazyLogger
from .jsonrpc import JsonRpc, get_value
from .translation import string_load
from .datamanager import DataManager
from .utils import get_art, double_urlencode
from .kodi_utils import HomeWindow

downloadUtils = DownloadUtils()
log = LazyLogger(__name__)


class CacheArtwork(threading.Thread):

    stop_all_activity = False

    def __init__(self):
        log.debug("CacheArtwork init")
        self.stop_all_activity = False
        super(CacheArtwork, self).__init__()

    def stop_activity(self):
        self.stop_all_activity = True

    def run(self):
        log.debug("CacheArtwork background thread started")
        last_update = 0
        home_window = HomeWindow()
        settings = xbmcaddon.Addon()
        latest_content_hash = "never"
        check_interval = int(settings.getSetting('cacheImagesOnScreenSaver_interval'))
        check_interval = check_interval * 60
        monitor = xbmc.Monitor()
        monitor.waitForAbort(5)

        while not self.stop_all_activity and not monitor.abortRequested() and xbmc.getCondVisibility("System.ScreenSaverActive"):
            content_hash = home_window.get_property("embycon_widget_reload")
            if (check_interval != 0 and (time.time() - last_update) > check_interval) or (latest_content_hash != content_hash):
                log.debug("CacheArtwork background thread - triggered")
                if monitor.waitForAbort(10):
                    break
                if self.stop_all_activity or monitor.abortRequested():
                    break
                self.cache_artwork_background()
                last_update = time.time()
                latest_content_hash = content_hash

            monitor.waitForAbort(5)

        log.debug("CacheArtwork background thread exited : stop_all_activity : {0}".format(self.stop_all_activity))

    @staticmethod
    def delete_cached_images(item_id):
        log.debug("cache_delete_for_links")

        progress = xbmcgui.DialogProgress()
        progress.create(string_load(30281))
        progress.update(30, string_load(30347))

        item_image_url_part = "Items/%s/Images/" % item_id
        item_image_url_part = item_image_url_part.replace("/", "%2f")
        log.debug("texture ids: {0}".format(item_image_url_part))

        # is the web server enabled
        web_query = {"setting": "services.webserver"}
        result = JsonRpc('Settings.GetSettingValue').execute(web_query)
        xbmc_webserver_enabled = result['result']['value']
        if not xbmc_webserver_enabled:
            xbmcgui.Dialog().ok(string_load(30294), string_load(30295))
            return

        params = {"properties": ["url"]}
        json_result = JsonRpc('Textures.GetTextures').execute(params)
        textures = json_result.get("result", {}).get("textures", [])
        log.debug("texture ids: {0}".format(textures))

        progress.update(70, string_load(30346))

        delete_count = 0
        for texture in textures:
            texture_id = texture["textureid"]
            texture_url = texture["url"]
            if item_image_url_part in texture_url:
                delete_count += 1
                log.debug("removing texture id: {0}".format(texture_id))
                params = {"textureid": int(texture_id)}
                JsonRpc('Textures.RemoveTexture').execute(params)

        del textures

        progress.update(100, string_load(30125))
        progress.close()

        xbmcgui.Dialog().ok(string_load(30281), '{}: {}'.format(string_load(30344), delete_count))

    def cache_artwork_interactive(self):
        log.debug("cache_artwork_interactive")

        xbmcplugin.endOfDirectory(int(sys.argv[1]), cacheToDisc=False)

        # is the web server enabled
        web_query = {"setting": "services.webserver"}
        result = JsonRpc('Settings.GetSettingValue').execute(web_query)
        xbmc_webserver_enabled = result['result']['value']
        if not xbmc_webserver_enabled:
            xbmcgui.Dialog().ok(string_load(30294), '{} - {}'.format(string_load(30295), string_load(30355)))
            xbmc.executebuiltin('ActivateWindow(servicesettings)')
            return

        result_report = []

        # ask questions
        question_delete_unused = xbmcgui.Dialog().yesno(string_load(30296), string_load(30297))
        question_cache_images = xbmcgui.Dialog().yesno(string_load(30299), string_load(30300))

        delete_canceled = False
        # now do work - delete unused
        if question_delete_unused:
            delete_pdialog = xbmcgui.DialogProgress()
            delete_pdialog.create(string_load(30298), "")
            index = 0

            params = {"properties": ["url"]}
            json_result = JsonRpc('Textures.GetTextures').execute(params)
            textures = json_result.get("result", {}).get("textures", [])

            jellyfin_texture_urls = self.get_jellyfin_artwork(delete_pdialog)

            log.debug("kodi textures: {0}".format(textures))
            log.debug("jellyfin texture urls: {0}".format(jellyfin_texture_urls))

            if jellyfin_texture_urls is not None:

                unused_texture_ids = set()
                for texture in textures:
                    url = texture.get("url")
                    url = unquote(url)
                    url = url.replace("image://", "")
                    url = url[0:-1]
                    if url.find("/") > -1 and url not in jellyfin_texture_urls or url.find("localhost:24276") > -1:
                        unused_texture_ids.add(texture["textureid"])

                total = len(unused_texture_ids)
                log.debug("unused texture ids: {0}".format(unused_texture_ids))

                for texture_id in unused_texture_ids:
                    params = {"textureid": int(texture_id)}
                    JsonRpc('Textures.RemoveTexture').execute(params)
                    percentage = int((float(index) / float(total)) * 100)
                    message = "%s of %s" % (index, total)
                    delete_pdialog.update(percentage, message)

                    index += 1
                    if delete_pdialog.iscanceled():
                        delete_canceled = True
                        break

                result_report.append(string_load(30385) + str(len(textures)))
                result_report.append(string_load(30386) + str(len(unused_texture_ids)))
                result_report.append(string_load(30387) + str(index))

            del textures
            del jellyfin_texture_urls
            del unused_texture_ids
            delete_pdialog.close()
            del delete_pdialog

        if delete_canceled:
            xbmc.sleep(2000)

        # now do work - cache images
        if question_cache_images:
            cache_pdialog = xbmcgui.DialogProgress()
            cache_pdialog.create(string_load(30301), "")
            cache_report = self.cache_artwork(cache_pdialog)
            cache_pdialog.close()
            del cache_pdialog
            if cache_report:
                result_report.extend(cache_report)

        if len(result_report) > 0:
            msg = "\r\n".join(result_report)
            xbmcgui.Dialog().textviewer(string_load(30125), msg, usemono=True)

    def cache_artwork_background(self):
        log.debug("cache_artwork_background")
        dp = xbmcgui.DialogProgressBG()
        dp.create(string_load(30301), "")
        result_text = None
        try:
            result_text = self.cache_artwork(dp)
        except Exception as err:
            log.error("Cache Images Failed : {0}".format(err))
        dp.close()
        del dp
        if result_text is not None:
            log.debug("Cache Images reuslt : {0}".format(" - ".join(result_text)))

    def get_jellyfin_artwork(self, progress):
        log.debug("get_jellyfin_artwork")

        url = ""
        url += "{server}/Users/{userid}/Items"
        url += "?Recursive=true"
        url += "&EnableUserData=False"
        url += "&Fields=BasicSyncInfo"
        url += "&IncludeItemTypes=Movie,Series,Episode,BoxSet"
        url += "&ImageTypeLimit=1"
        url += "&format=json"

        data_manager = DataManager()
        results = data_manager.get_content(url)
        if results is None:
            results = []

        if isinstance(results, dict):
            results = results.get("Items")

        server = downloadUtils.get_server()
        log.debug("Jellyfin Item Count Count: {0}".format(len(results)))

        if self.stop_all_activity:
            return None

        progress.update(0, string_load(30359))

        texture_urls = set()

        for item in results:
            art = get_art(item, server)
            for art_type in art:
                texture_urls.add(art[art_type])

        return texture_urls

    def cache_artwork(self, progress):
        log.debug("cache_artwork")

        # is the web server enabled
        if not get_value("services.webserver"):
            log.error("Kodi web server not enabled, can not cache images")
            return

        # get the port
        xbmc_port = get_value("services.webserverport")
        log.debug("xbmc_port: {0}".format(xbmc_port))

        # get the user
        xbmc_username = get_value("services.webserverusername")
        log.debug("xbmc_username: {0}".format(xbmc_username))

        # get the password
        xbmc_password = get_value("services.webserverpassword")

        progress.update(0, string_load(30356))

        params = {"properties": ["url"]}
        json_result = JsonRpc('Textures.GetTextures').execute(params)
        textures = json_result.get("result", {}).get("textures", [])
        log.debug("Textures.GetTextures Count: {0}".format(len(textures)))

        if self.stop_all_activity:
            return

        progress.update(0, string_load(30357))

        texture_urls = set()
        for texture in textures:
            url = texture.get("url")
            url = unquote(url)
            url = url.replace("image://", "")
            url = url[0:-1]
            texture_urls.add(url)

        del textures
        del json_result

        log.debug("texture_urls Count: {0}".format(len(texture_urls)))

        if self.stop_all_activity:
            return

        progress.update(0, string_load(30358))

        jellyfin_texture_urls = self.get_jellyfin_artwork(progress)
        if jellyfin_texture_urls is None:
            return

        missing_texture_urls = set()

        for image_url in jellyfin_texture_urls:
            if image_url not in texture_urls and not image_url.endswith("&Tag=") and len(image_url) > 0:
                missing_texture_urls.add(image_url)

            if self.stop_all_activity:
                return

        log.debug("texture_urls: {0}".format(texture_urls))
        log.debug("missing_texture_urls: {0}".format(missing_texture_urls))
        log.debug("Number of existing textures: {0}".format(len(texture_urls)))
        log.debug("Number of missing textures: {0}".format(len(missing_texture_urls)))

        kodi_http_server = "localhost:" + str(xbmc_port)
        headers = {}
        if xbmc_password:
            auth = "%s:%s" % (xbmc_username, xbmc_password)
            headers = {'Authorization': 'Basic %s' % base64.b64encode(auth)}

        total = len(missing_texture_urls)

        count_done = 0
        for index, get_url in enumerate(missing_texture_urls, 1):
            url = double_urlencode(get_url)
            kodi_texture_url = ("/image/image://%s" % url)
            log.debug("kodi_texture_url: {0}".format(kodi_texture_url))

            percentage = int((float(index) / float(total)) * 100)
            message = "%s of %s" % (index, total)
            progress.update(percentage, message)

            cache_url = "http://%s%s" % (kodi_http_server, kodi_texture_url)
            data = requests.get(cache_url, timeout=20, headers=headers)

            if data.status_code == 200:
                count_done += 1
            log.debug("Get Image Result: {0}".format(data.status_code))

            if isinstance(progress, xbmcgui.DialogProgress) and progress.iscanceled():
                break

            if self.stop_all_activity:
                break

        result_report = []
        result_report.append(string_load(30302) + str(len(texture_urls)))
        result_report.append(string_load(30303) + str(len(missing_texture_urls)))
        result_report.append(string_load(30304) + str(count_done))
        return result_report
