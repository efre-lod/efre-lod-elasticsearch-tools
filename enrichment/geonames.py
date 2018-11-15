#!/usr/bin/python3

import argparse
import json
import sys
import requests
from es2json import esgenerator,isint,litter,eprint,isfloat
from rdflib import Graph



def get_gnid(rec):
    changed=False
    lat=rec["geo"].get("latitude")
    lng=rec["geo"].get("longitude")
    if lat and not isfloat(lat) and isfloat(lat[1:]):
        lat=lat[1:]
    if lng and not isfloat(lng) and isfloat(lng[1:]):
        lng=lng[1:]
    r=requests.get("http://api.geonames.org/findNearbyJSON?lat="+lat+"&lng="+lng+"&username=slub")
    if r.ok:
        for geoNameRecord in r.json().get("geonames"):
            if rec.get("name") in geoNameRecord.get("name"):    #match!
                rec["sameAs"]=litter(rec.get("sameAs"),"http://www.geonames.org/"+str(geoNameRecord.get("geonameId"))+"/")
                changed=True
    if changed:
        return rec
    else:
        return None
        
    

if __name__ == "__main__":
    parser=argparse.ArgumentParser(description='enrich ES by GN!')
    parser.add_argument('-host',type=str,default="127.0.0.1",help='hostname or IP-Address of the ElasticSearch-node to use, default is localhost.')
    parser.add_argument('-port',type=int,default=9200,help='Port of the ElasticSearch-node to use, default is 9200.')
    parser.add_argument('-index',type=str,help='ElasticSearch Search Index to use')
    parser.add_argument('-type',type=str,help='ElasticSearch Search Index Type to use')
    parser.add_argument("-id",type=str,help="retrieve single document (optional)")
    parser.add_argument('-stdin',action="store_true",help="get data from stdin")
    parser.add_argument('-pipeline',action="store_true",help="output every record (even if not enriched) to put this script into a pipeline")
    parser.add_argument('-server',type=str,help="use http://host:port/index/type/id?pretty. overwrites host/port/index/id/pretty") #no, i don't steal the syntax from esbulk...
    args=parser.parse_args()
    tabbing=None
    if args.server:
        slashsplit=args.server.split("/")
        args.host=slashsplit[2].rsplit(":")[0]
        if isint(args.server.split(":")[2].rsplit("/")[0]):
            args.port=args.server.split(":")[2].split("/")[0]
        args.index=args.server.split("/")[3]
        if len(slashsplit)>4:
            args.type=slashsplit[4]
        if len(slashsplit)>5:
            if "?pretty" in args.server:
                tabbing=4
                args.id=slashsplit[5].rsplit("?")[0]
            else:
                args.id=slashsplit[5]
    if args.stdin:
        for line in sys.stdin:
            rec=json.loads(line)
            if rec.get("geo"):
                newrec=get_gnid(rec)
                rec=newrec
            if args.pipeline or newrec:
                print(json.dumps(rec,indent=tabbing))
    else:
        for rec in esgenerator(host=args.host,port=args.port,index=args.index,type=args.type,headless=True,body={"query":{"exists":{"field":"geo"}}}):
            newrec=get_gnid(rec)
            if newrec:
                rec=newrec
            if args.pipeline or newrec:
                print(json.dumps(rec,indent=tabbing))
            