# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import sys
import threading
import platform
import time
import subprocess
import configparser
import json
import web
from datetime import datetime, timedelta, timezone

if len(sys.argv) != 2:
    print ("Usage: swift-cluster-status.py CONFIG")
    sys.exit()

URL = (
    '/', 'clusterStatus'
)

TEMPLATE='cluster_replication_status{{type="{type}", metric_type="all_copies_in_place", metric="percent"}} {percent:.2f}\n'

config = configparser.ConfigParser()
config.read(sys.argv[1])

EXEC_DIR  = config.get('DEFAULT', 'exec_dir', fallback='/usr/local/bin')
DATA_DIR  = config.get('DEFAULT', 'data_dir', fallback='/var/lib/swift')
TIME_WAIT = config.get('DEFAULT', 'inspection_frequency', fallback = 360)

BIND_PORT = config.get('DEFAULT', 'bind_port', fallback = '7000')
BIND_IP   = config.get('DEFAULT', 'bind_ip', fallback = '0.0.0.0')

lock = threading.Lock()

class ClusterStatusWorker(threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self)

    def run(self):
        while True:
            stat = ''
            try:
                output = subprocess.run(
                    [EXEC_DIR+"/swift-dispersion-report", "-j", '/opt/swift/etc/dispersion.conf'],
                    capture_output=True,
                    check=True,
                )
                status = json.loads( output.stdout.decode())

                stat += TEMPLATE.format(type='buckets', percent = status['container']['pct_found'])
                stat += TEMPLATE.format(type='objects', percent = status['object']['pct_found'])

                with open(DATA_DIR + '/all_copies_in_place.tmp', 'w+') as f:
                   f.write(stat)

            except subprocess.CalledProcessError:
                print ("Error "+ str(datetime.utcfromtimestamp(timestamp)) + " subprocess.CalledProcessError in GET method")

            lock.acquire()
            os.replace(DATA_DIR + '/all_copies_in_place.tmp', DATA_DIR + '/all_copies_in_place')
            lock.release()
            time.sleep(int(TIME_WAIT))


class clusterStatus:
    def GET(self):
        obj_stat = ''
        if os.path.exists(DATA_DIR + '/all_copies_in_place'):
            timestamp = time.time()

            obj_stat += '# ' + str(datetime.utcfromtimestamp(timestamp)) + '\n'
            obj_stat += '# HELP S3 cluster replication status \n'
            obj_stat += '# Metric type "all_copies_in_place" measurement \n'

            lock.acquire()
            with open(DATA_DIR + '/all_copies_in_place', "r+") as f:
               for d in f:
                   obj_stat += d
            lock.release()

        return obj_stat


if __name__ == "__main__":
    try:
        thread = ClusterStatusWorker()
        thread.start()
    except (KeyboardInterrupt, SystemExit):
        thread.stop()
        sys.exit()

    app = web.application(URL, globals())
    web.httpserver.runsimple(app.wsgifunc(),( BIND_IP, int(BIND_PORT) ))
