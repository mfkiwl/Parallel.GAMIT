"""
Project: Parallel.Archive
Date: 02/16/2017
Author: Demian D. Gomez
"""

import pyArchiveStruct
import dbConnection
import pyDate
import pyRinex
import pp
from shutil import rmtree
import datetime
import copy
import os
import pyOTL
import pyStationInfo
import sys
import pySp3
import pyPPP
from tqdm import tqdm
import getopt
import numpy
import pyOptions
import time
import Utils

class callback_class():
    def __init__(self, pbar):
        self.errors = None
        self.pbar = pbar

    def callbackfunc(self, args):
        msg = args
        self.erros = msg
        self.pbar.update(1)

def verify_rinex_date_multiday(cnn, date, rinexinfo, Config):
    # function to verify if rinex is multiday or if the file is from the date it was stored in the archive
    # returns true if parent process can continue with insert
    # returns false if file had to be moved from the archive (date != rinex.date or multiday file)

    # check if rinex is a multiday file (rinex with more than one day of observations)
    if rinexinfo.multiday:

        # move all the files to the repository, delete the crinex from the archive, log the event
        rnxlist = []
        for rnx in rinexinfo.multiday_rnx_list:
            rnxlist.append(rnx.rinex)
            # some other file, move it to the repository
            retry_folder = os.path.join(Config.repository_data_in_retry, rnx.date.yyyy() + '/' + rnx.date.ddd())
            rnx.compress_local_copyto(retry_folder)

        # if the file corresponding to this session is found, assign its object to rinexinfo
        cnn.insert_warning(
            '%s was a multi-day rinex file. The following rinex files where generated and moved to the repository/data_in_retry: %s. The file %s (which did not enter the database) was deleted from the archive.' % (
            rinexinfo.origin_file, ','.join(rnxlist), rinexinfo.crinex))
        # remove crinex from archive
        os.remove(rinexinfo.origin_file)

        return False

    # compare the date of the rinex with the date in the archive
    if not date == rinexinfo.date:
        # move the file out of the archive because it's in the wrong spot (wrong folder, wrong name, etc)
        # let pyArchiveService fix the issue
        retry_folder = os.path.join(Config.repository_data_in_retry, date.yyyy() + '/' + date.ddd())
        # move the crinex out of the archive
        rinexinfo.move_origin_file(retry_folder)

        cnn.insert_warning(
            'The date in the archive for ' + rinexinfo.NetworkCode + '.' + rinexinfo.StationCode + '::' +
            rinexinfo.rinex + ' (' + date.yyyyddd() + ') does not agree with the mean session date (' +
            rinexinfo.date.yyyyddd() + '). The file was moved to the repository/data_in_retry and should be analyzed later by pyArchiveService.')

        return False

    return True

def check_rinex_timespan_int(rinex, stn):

    # how many seconds difference between the rinex file and the record in the db
    stime_diff = abs((stn['ObservationSTime'] - rinex.datetime_firstObs).total_seconds())
    etime_diff = abs((stn['ObservationETime'] - rinex.datetime_lastObs).total_seconds())

    # at least four minutes different on each side
    if stime_diff <= 240 and etime_diff <= 240 and stn['Interval'] == rinex.interval:
        return False
    else:
        return True


def try_insert(NetworkCode, StationCode, year, doy, rinex, Config):

    import traceback

    try:
        # get the rinex file name
        filename = rinex.split('/')[-1].replace('d.Z', 'o')

        cnn = dbConnection.Cnn("gnss_data.cfg")

        # build the archive level sql string
        rs = cnn.query(
            'SELECT * FROM rinex WHERE "NetworkCode" = \'%s\' AND "StationCode" = \'%s\' AND "ObservationYear" = \'%s\' AND "ObservationDOY" = \'%s\''
            % (NetworkCode, StationCode, year, doy))

        if rs.ntuples() == 0:
            # no record found, new rinex file for this day
            # examine the rinex
            rinexinfo = pyRinex.ReadRinex(NetworkCode,StationCode,rinex)

            date = pyDate.Date(year=year,doy=doy)

            # verify that the rinex is from this date and that is not a multiday file
            if verify_rinex_date_multiday(cnn, date, rinexinfo, Config):
                try:
                    # create the insert statement
                    cnn.insert('rinex', rinexinfo.record)
                except dbConnection.dbErrInsert:
                    # insert duplicate values: two parallel processes tried to insert different filenames of the same station
                    # to the db: insert to the rinex_extra and let the parent process decide (in serial mode)
                    cnn.insert('rinex_extra', rinexinfo.record)
        else:
            # this record was in the database.
            # save the rinex table record
            rnx = rs.dictresult()[0]

            # Check if the filename is the same
            rs = cnn.query('SELECT * FROM rinex WHERE "NetworkCode" = \'%s\' AND "StationCode" = \'%s\' AND "ObservationYear" = \'%s\' AND "ObservationDOY" = \'%s\' AND "Filename" = \'%s\''
                % (NetworkCode, StationCode, year, doy, filename))

            # if there is a record, it's the same file being reprocessed. Just ignore it
            if rs.ntuples() == 0:
                # if no records came back, there might be a duplicate rinex with a different filename
                # or this could be another session of the same day

                # first, verify that this file isn't in the rinex_extra table
                # if it's in the table, do nothing
                rs = cnn.query('SELECT * FROM rinex_extra WHERE "NetworkCode" = \'%s\' AND "StationCode" = \'%s\' AND "ObservationYear" = \'%s\' AND "ObservationDOY" = \'%s\' AND "Filename" = \'%s\''
                    % (NetworkCode, StationCode, year, doy, filename))

                if rs.ntuples() == 0:
                    # the file was not found the rinex_extra

                    rinexinfo = pyRinex.ReadRinex(NetworkCode, StationCode, rinex)

                    date = pyDate.Date(year=year, doy=doy)

                    if verify_rinex_date_multiday(cnn, date, rinexinfo, Config):

                        # we need to check if both files are the same or not
                        # if the file has the same time span as the primary rinex in the db and the same interval,
                        # do not add it to the database
                        if check_rinex_timespan_int(rinexinfo, rnx):
                            # insert to rinex_extra. Will be processed later (not in parallel)
                            cnn.insert('rinex_extra', rinexinfo.record)
                        else:
                            # do not remove for the moment
                            # log the event
                            #os.remove()
                            cnn.insert_info('The archive crinex file %s had the same timespan and sampling interval than %s.%s %s. The file was not added to rinex_extra but it was not removed from the archive. In a future release, these files will be deleted.' % (rinex,NetworkCode,StationCode,date.yyyyddd()))


    except pyRinex.pyRinexException as e:

        cnn.insert_warning('During ' + rinex + ' :' + str(e))
        return

    except:

        return traceback.format_exc() + ' processing rinex: ' + NetworkCode + ' ' + StationCode

def process_extra_rinex(NetworkCode, StationCode, year, doy, rinex):

    import traceback

    try:
        cnn = dbConnection.Cnn("gnss_data.cfg")

        # load the current_rinex
        rs = cnn.query(
            'SELECT * FROM rinex WHERE "NetworkCode" = \'%s\' AND "StationCode" = \'%s\' AND "ObservationYear" = \'%s\' AND "ObservationDOY" = \'%s\''
            % (NetworkCode, StationCode, year, doy))

        # save the information of the current rinex in the db
        current_rinex = rs.dictresult()[0]

        rinexinfo = pyRinex.ReadRinex(NetworkCode, StationCode, rinex)

        if (current_rinex['ObservationETime'] - current_rinex['ObservationSTime']).total_seconds() < \
                (rinexinfo.datetime_firstObs - rinexinfo.datetime_firstObs).total_seconds():
            # new file larger than previous, update rinex table
            cnn.begin_transac()

            # this dictionary will be updated
            update_dict = copy.deepcopy(current_rinex)

            cnn.update('rinex', update_dict, rinexinfo.record)

            # update the record in rinex_extra (put in the rinex file we had in rinex)
            cnn.insert('rinex_extra', current_rinex)
            # delete the other one
            cnn.delete('rinex_extra', update_dict)

            # generate an info event saying what we did
            cnn.insert_info(
                'A longer rinex file (' + rinexinfo.rinex + ') was found for ' + NetworkCode + ' ' + StationCode + ' '
                + rinexinfo.date.yyyyddd() + ' and replaced file ' + current_rinex['Filename'])

            cnn.commit_transac()

    except pyRinex.pyRinexException as e:

        cnn.insert_warning('Processing EXTRA RINEX during ' + rinex + ' :' + str(e))
        return

    except:
        traceback.format_exc() + ' (process_extra_rinex) processing: ' + NetworkCode + ' ' + StationCode

def ecef2lla(ecefArr):
    # convert ECEF coordinates to LLA
    # test data : test_coord = [2297292.91, 1016894.94, -5843939.62]
    # expected result : -66.8765400174 23.876539914 999.998386689

    x = float(ecefArr[0])
    y = float(ecefArr[1])
    z = float(ecefArr[2])

    a = 6378137
    e = 8.1819190842622e-2

    asq = numpy.power(a, 2)
    esq = numpy.power(e, 2)

    b = numpy.sqrt(asq * (1 - esq))
    bsq = numpy.power(b, 2)

    ep = numpy.sqrt((asq - bsq) / bsq)
    p = numpy.sqrt(numpy.power(x, 2) + numpy.power(y, 2))
    th = numpy.arctan2(a * z, b * p)

    lon = numpy.arctan2(y, x)
    lat = numpy.arctan2((z + numpy.power(ep, 2) * b * numpy.power(numpy.sin(th), 3)),
                     (p - esq * a * numpy.power(numpy.cos(th), 3)))
    N = a / (numpy.sqrt(1 - esq * numpy.power(numpy.sin(lat), 2)))
    alt = p / numpy.cos(lat) - N

    lon = lon * 180 / numpy.pi
    lat = lat * 180 / numpy.pi

    return numpy.array([lat]), numpy.array([lon]), numpy.array([alt])

def obtain_otl(NetworkCode, StationCode, archive_path, brdc_path, options, sp3types, sp3altrn):

    import traceback

    outmsg = []
    x = []
    y = []
    z = []

    try:
        cnn = dbConnection.Cnn("gnss_data.cfg")

        pyArchive = pyArchiveStruct.RinexStruct(cnn)

        # assumes that the files in the db are correct. We take 5 records from the time span (evenly spaced)
        stn = cnn.query('SELECT * FROM (SELECT row_number() OVER() as rnum, r.* FROM rinex as r WHERE "NetworkCode" = \'%s\' '
                        'AND "StationCode" = \'%s\' ORDER BY "ObservationSTime") AS rr '
                        'WHERE (rnum %% ((SELECT count(*) FROM rinex as r WHERE "NetworkCode" = \'%s\' AND "StationCode" = \'%s\')/10)) = 0' % (
            NetworkCode, StationCode, NetworkCode, StationCode))

        tblrinex = stn.dictresult()

        for dbRinex in tblrinex:
            # obtain the path to the crinex
            file = pyArchive.build_rinex_path(NetworkCode, StationCode, dbRinex['ObservationYear'],
                                              dbRinex['ObservationDOY'])
            # read the crinex
            try:
                Rinex = pyRinex.ReadRinex(dbRinex['NetworkCode'], dbRinex['StationCode'],
                                          os.path.join(archive_path, file))
            except pyRinex.pyRinexException as e:
                # problem loading this file, try another one
                continue
            except:
                return traceback.format_exc() + ' processing: ' + NetworkCode + ' ' + StationCode
            else:

                try:
                    # run ppp without otl and met and in non-strict mode
                    ppp = pyPPP.RunPPP(Rinex,'' , options, sp3types, sp3altrn, Rinex.antOffset, False, False)

                    ppp.exec_ppp()
                except pyPPP.pyRunPPPException:
                    continue

                x.append(ppp.x)
                y.append(ppp.y)
                z.append(ppp.z)

        # average the x y z values
        if len(x) > 0:
            x = numpy.array(x)
            y = numpy.array(y)
            z = numpy.array(z)

            x = numpy.mean(x[abs(x - numpy.mean(x)) < 2 * numpy.std(x)])
            y = numpy.mean(y[abs(y - numpy.mean(y)) < 2 * numpy.std(y)])
            z = numpy.mean(z[abs(z - numpy.mean(z)) < 2 * numpy.std(z)])

            lat,lon,h = ecef2lla([x,y,z])

            # calculate the otl parameters if the auto_coord returned a valid position
            otl = pyOTL.OceanLoading(StationCode, options['grdtab'], options['otlgrid'])
            coeff = otl.calculate_otl_coeff(x=x, y=y, z=z)

            # update record in the database
            cnn.query('UPDATE stations SET "auto_x" = %.3f, "auto_y" = %.3f, "auto_z" = %.3f, "lat" = %.8f, "lon" = %.8f, "height" = %.3f, "Harpos_coeff_otl" = \'%s\' WHERE "NetworkCode" = \'%s\' AND "StationCode" = \'%s\'' % (x, y, z, lat[0], lon[0], h[0], coeff, NetworkCode, StationCode))
        else:
            outmsg = 'Could not obtain a coordinate/otl coefficients for ' + NetworkCode + ' ' + StationCode + ' after 20 tries. Maybe there where few valid RINEX files or could not find an ephemeris file'

    except pyOTL.pyOTLException as e:
        return "Error while calculating OTL for " + NetworkCode + " " + StationCode + ": " + str(e)
    except:
        outmsg = traceback.format_exc() + ' processing otl: ' + NetworkCode + ' ' + StationCode

    return outmsg


def insert_stninfo(NetworkCode, StationCode, stninfofile):

    import traceback

    errors = []

    try:
        cnn = dbConnection.Cnn("gnss_data.cfg")
    except:
        return traceback.format_exc() + ' insert_stninfo: ' + NetworkCode + ' ' + StationCode

    try:
        stnInfo = pyStationInfo.StationInfo(cnn,NetworkCode,StationCode, allow_empty=True)
        stninfo = stnInfo.parse_station_info(stninfofile)

    except pyStationInfo.pyStationInfoException as e:
        return traceback.format_exc() + ' insert_stninfo: ' + NetworkCode + ' ' + StationCode

    # insert all the receivers and antennas in the db
    for stn in stninfo:
        # there is a racing condition in this part due to many instances trying to insert the same receivers at the same time
        try:
            rec = cnn.query('SELECT * FROM receivers WHERE "ReceiverCode" = \'%s\'' % (stn['ReceiverCode']))
            if rec.ntuples() == 0:
                cnn.insert('receivers', ReceiverCode=stn['ReceiverCode'])
        except dbConnection.dbErrInsert:
            sys.exc_clear()

        try:
            rec = cnn.query('SELECT * FROM antennas WHERE "AntennaCode" = \'%s\'' % (stn['AntennaCode']))
            if rec.ntuples() == 0:
                cnn.insert('antennas', AntennaCode=stn['AntennaCode'])
        except dbConnection.dbErrInsert:
            sys.exc_clear()

    # ready to insert stuff to station info table
    for stn in stninfo:
        if stn.get('StationCode').lower() == StationCode:
            try:
                stnInfo.InsertStationInfo(stn)
            except pyStationInfo.pyStationInfoException as e:
                errors.append(str(e))
            except:
                errors.append(traceback.format_exc() + ' insert_stninfo: ' + NetworkCode + ' ' + StationCode)
                continue

    if not errors:
        return
    else:
        return '\n\n'.join(errors)


def remove_from_archive(cnn, record, Rinex, Config):

    # do not make thing very complex here, just move it out from the archive
    retry_folder = os.path.join(Config.repository_data_in_retry, Rinex.date.yyyy() + '/' + Rinex.date.ddd())
    Rinex.move_origin_file(retry_folder)

    cnn.begin_transac()
    # delete this rinex entry from the database
    cnn.delete('rinex', record)
    # are there any rinex extra? Maybe they are correct.
    rs = cnn.query(
        'SELECT * FROM rinex_extra WHERE "NetworkCode" = \'%s\' AND "StationCode" = \'%s\' AND "ObservationYear" = %i AND "ObservationDOY" = %i' % (
        record['NetworkCode'], record['StationCode'], record['ObservationYear'], record['ObservationDOY']))

    if rs.ntuples() > 0:
        rnx = rs.dictresult()

        cnn.insert_warning(
            'After running PPP it was found that the rinex file %s does not belong to %s.%s. This file will be removed from the rinex table (and a rinex_extra %s was promoted to rinex) and moved to the repository/data_in_retry to try to add it to the corresponding station.' % (
                Rinex.origin_file, record['NetworkCode'], record['StationCode'], rnx[0]['Filename']))

        cnn.insert('rinex', rnx[0])
        cnn.delete('rinex_extra', rnx[0])
        # on the next execution of pyScanArchive --ppp this file will be analyzed
    else:
        cnn.insert_warning(
            'After running PPP it was found that the rinex file %s does not belong to %s.%s. This file will be removed from the rinex table (no rinex_extra found to be promoted to rinex) and moved to the repository/data_in_retry to add it to the corresponding station.' % (
                Rinex.origin_file, record['NetworkCode'], record['StationCode']))

    cnn.commit_transac()
    return


def execute_ppp(record, rinex_path, Config):

    import traceback

    NetworkCode = record['NetworkCode']
    StationCode = record['StationCode']
    year = record['ObservationYear']
    doy = record['ObservationDOY']

    # create a temp folder in production to put the orbit in
    # we need to check the RF of the orbit to see if we have this solution in the DB
    try:
        cnn = dbConnection.Cnn('gnss_data.cfg')

        rootdir = 'production/' + NetworkCode + '/' + StationCode

        try:
            if not os.path.exists(rootdir):
                os.makedirs(rootdir)
        except OSError:
            # folder exists from a concurring instance, ignore the error
            sys.exc_clear()
        except:
            raise

        date = pyDate.Date(year=year,doy=doy)
        orbit = pySp3.GetSp3Orbits(Config.options['sp3'], date, Config.sp3types, rootdir)

        # check to see if record exists for this file in ppp_soln
        ppp_soln = cnn.query('SELECT * FROM ppp_soln WHERE "NetworkCode" = \'%s\' AND "StationCode" = \'%s\' AND '
                             '"Year" = %s AND "DOY" = %s AND "ReferenceFrame" = \'%s\''
                             % (NetworkCode, StationCode, year, doy, orbit.RF))

        if ppp_soln.ntuples() == 0:

            # load the stations record to get the OTL params
            rs_stn = cnn.query('SELECT * FROM stations WHERE "NetworkCode" = \'%s\' AND "StationCode" = \'%s\'' % (
                                NetworkCode, StationCode))
            stn = rs_stn.dictresult()

            # RINEX FILE TO BE PROCESSED
            Rinex = pyRinex.ReadRinex(NetworkCode, StationCode, rinex_path)

            stninfo = pyStationInfo.StationInfo(cnn, NetworkCode, StationCode, Rinex.date)

            Rinex.normalize_header(StationInfo=stninfo, x=stn[0]['auto_x'], y=stn[0]['auto_y'], z=stn[0]['auto_z'])

            ppp = pyPPP.RunPPP(Rinex, stn[0]['Harpos_coeff_otl'], Config.options, Config.sp3types, Config.sp3altrn, stninfo.AntennaHeight)
            ppp.exec_ppp()

            # verify that the solution is from the station it claims to be
            Result, match, closest_stn = ppp.verify_spatial_coherence(cnn, StationCode)

            if Result:
                if match['NetworkCode'] == NetworkCode and match['StationCode'] == StationCode:
                    # the match agrees with the station-day that we THINK we are processing
                    # this check should not be necessary if the rinex went through Archive Service, since we
                    # already match rinex vs station
                    # but it's still here to prevent that a rinex imported by pyScanArchive (which assumes the rinex
                    # files belong to the network/station of the folder) doesn't get into the PPP table if it's not
                    # of the station it claims to be.

                    # insert record in DB
                    cnn.insert('ppp_soln', ppp.record)
                else:
                    remove_from_archive(cnn, record, Rinex, Config)
            else:
                remove_from_archive(cnn, record, Rinex, Config)

    except pyRinex.pyRinexException as e:
        cnn.insert_warning('Error in ReadRinex: ' + NetworkCode + ' ' + StationCode + ' ' + str(year) + ' ' + str(doy) + ': \n' + str(e))

    except pyPPP.pyRunPPPException as e:
        cnn.insert_warning('Error in PPP while processing: ' + NetworkCode + ' ' + StationCode + ' ' + str(year) + ' ' + str(doy) + ': \n' + str(e))

    except pyStationInfo.pyStationInfoException as e:
        cnn.insert_warning('pyStationInfoException while running pyPPPArchive: ' + str(e))

    except:
        return traceback.format_exc() + ' processing: ' + NetworkCode + ' ' + StationCode + ' ' + str(year) + ' ' + str(doy)


def output_handle(callback):

    messages = [outmsg.errors for outmsg in callback]

    # function to print any error that are encountered during parallel execution
    for msg in messages:
        if msg:
            f = open('errors_pyScanArchive.log','a')
            f.write('ON ' + datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S') + ' an unhandled error occurred:\n')
            f.write(msg + '\n')
            f.write('END OF ERROR =================== \n\n')
            f.close()

    return []

def scan_rinex(cnn, job_server, pyArchive, archive_path, Config):

    print " >> Analyzing the archive's structure..."
    archivefiles, path2rinex = pyArchive.scan_archive_struct(archive_path)

    print "   >> Beginning with the recursive search for CRINEX files..."
    submit = 0
    pbar = tqdm(total=len(archivefiles), ncols=80)

    callback = []
    for rinex, rinexpath in zip(archivefiles, path2rinex):

        valid, NetworkCode, StationCode, year, doy, _, _ = pyArchive.parse_archive_keys(rinex, key_filter=('network','station','year','doy'))

        if valid:

            # check existence of network in the db
            rs = cnn.query('SELECT * FROM networks WHERE "NetworkCode" = \'%s\'' % (NetworkCode))
            if rs.ntuples() == 0:
                cnn.insert('networks', NetworkCode=NetworkCode, NetworkName='UNK')

            # check existence of station in the db
            rs = cnn.query('SELECT * FROM stations WHERE "NetworkCode" = \'%s\' AND "StationCode" = \'%s\'' % (
                NetworkCode, StationCode))
            if rs.ntuples() == 0:
                # run grdtab to get the OTL parameters in HARPOS format and insert then in the db
                # use the current rinex to get an approximate coordinate
                cnn.insert('stations', NetworkCode=NetworkCode, StationCode=StationCode)

            # it was a valid archive entry, insert in database
            # print "About to execute "+rinexpath
            if Config.run_parallel:
                callback.append(callback_class(pbar))

                job_server.submit(try_insert, args=(NetworkCode, StationCode, year, doy, rinexpath, Config),
                                  depfuncs=(verify_rinex_date_multiday, check_rinex_timespan_int),
                                  modules=('dbConnection', 'pyDate', 'pyRinex', 'shutil'), callback=callback[submit].callbackfunc)
                submit += 1

                if submit > 300:
                    # when we submit more than 300 jobs, wait until this batch is complete
                    # print " >> Batch of 300 jobs sent to the queue. Waiting until complete..."
                    job_server.wait()
                    # handle any output messages during this batch
                    callback = output_handle(callback)
                    submit = 0
            else:
                callback.append(callback_class(pbar))
                callback[0].callbackfunc(try_insert(NetworkCode, StationCode, year, doy, rinexpath, Config))
                callback = output_handle(callback)

    if Config.run_parallel:
        job_server.wait()

    # handle any output messages during this batch
    output_handle(callback)
    pbar.close()

    if Config.run_parallel:
        print "\n"
        job_server.print_stats()

    return


def process_conflicts(cnn, pyArchive, archive_path):

    print " >> About to process RINEX conflicts..."

    rs = cnn.query('SELECT * FROM rinex_extra')
    records = rs.dictresult()

    for record in tqdm(records):
        crinexpath = pyArchive.build_rinex_path(record['NetworkCode'], record['StationCode'],
                                              record['ObservationYear'], record['ObservationDOY'])

        if crinexpath:
            # replace the rinex filename with the rinex_extra filename
            crinexpath = crinexpath.split('/')[:-1]
            crinexpath = os.path.join(os.path.join(archive_path, '/'.join(crinexpath)), record['Filename'][:-1] + 'd.Z')

            process_extra_rinex(record['NetworkCode'], record['StationCode'], record['ObservationYear'],
                                record['ObservationDOY'], crinexpath)

    return


def process_otl(cnn, job_server, run_parallel, archive_path, brdc_path, options, sp3types, sp3altrn):

    print ""
    print " >> Calculating coordinates and OTL for new stations..."
    rs = cnn.query('SELECT * FROM stations WHERE auto_x is null or auto_y is null or auto_z is null or "Harpos_coeff_otl" is null')
    #rs = cnn.query('SELECT * FROM stations WHERE "StationCode" = \'cjnt\' OR "StationCode" = \'bue2\'')
    records = rs.dictresult()

    pbar = tqdm(total=len(records), ncols=80)
    callback = []
    for record in records:
        NetworkCode = record['NetworkCode']
        StationCode = record['StationCode']

        if run_parallel:
            callback.append(callback_class(pbar))

            job_server.submit(obtain_otl, args=(NetworkCode, StationCode, archive_path, brdc_path, options, sp3types, sp3altrn),
                              depfuncs=(ecef2lla,),
                              modules=('dbConnection', 'pyRinex', 'pyArchiveStruct', 'pyOTL', 'pyPPP', 'numpy'),
                              callback=callback[-1].callbackfunc)
        else:
            callback.append(callback_class(pbar))
            callback[0].callbackfunc(obtain_otl(NetworkCode, StationCode, archive_path, brdc_path, options, sp3types, sp3altrn))
            callback = output_handle(callback)

    if run_parallel:
        job_server.wait()

    # handle any output messages during this batch
    output_handle(callback)
    pbar.close()

    if run_parallel:
        print '\n'
        job_server.print_stats()

    return


def scan_station_info(job_server, run_parallel, pyArchive, archive_path):

    print " >> Searching for station info files in the archive..."

    stninfo, path2stninfo = pyArchive.scan_archive_struct_stninfo(archive_path)

    print "   >> Processing Station Info files..."

    pbar = tqdm(total=len(stninfo), ncols=80)
    callback = []
    for stninfofile, stninfopath in zip(stninfo,path2stninfo):

        valid, NetworkCode, StationCode, _, _, _, _ = pyArchive.parse_archive_keys(stninfofile, key_filter=('network','station'))

        if valid:
            # we were able to get the network and station code, add it to the database
            if run_parallel:
                callback.append(callback_class(pbar))
                job_server.submit(insert_stninfo, args=(NetworkCode,StationCode,stninfopath),
                                  modules=('dbConnection', 'pyStationInfo', 'sys', 'datetime', 'pyDate'),
                                  callback=callback[-1].callbackfunc)
            else:
                callback.append(callback_class(pbar))
                callback[0].callbackfunc(insert_stninfo(NetworkCode,StationCode,stninfopath))
                callback = output_handle(callback)

    if run_parallel:
        job_server.wait()

    # handle any output messages during this batch
    output_handle(callback)
    pbar.close()

    if run_parallel:
        print '\n'
        job_server.print_stats()

    return

def scan_station_info_manual(cnn, pyArchive, stn_info_path, stn_info_stn, stn_info_net, stdin=None):


    print " >> Manual scan of station info files in " + stn_info_path

    NetworkCode = stn_info_net

    if stdin:
        stn_info_obj = pyStationInfo.StationInfo(cnn)
        stn_list = stn_info_obj.parse_station_info(stdin)

        if stn_info_stn.lower() == 'all':
            # parse the station info and get all the stations to go one by one
            print "   >> All stations from station info requested to be added using network code %s" % (NetworkCode)

            for stn in tqdm(stn_list, total=len(stn_list)):
                tqdm.write("     >> Processing %s using network code %s" % (stn['StationCode'].lower(), NetworkCode))
                out = insert_stninfo(NetworkCode, stn.get('StationCode').lower(), stdin)
                if out:
                    tqdm.write(out)
        else:
            if ',' in stn_info_stn:
                stations = stn_info_stn.lower().split(',')
            else:
                stations = [stn_info_stn.lower()]

            for StationCode in tqdm(stations, total=len(stations)):
                if StationCode in [stn['StationCode'].lower() for stn in stn_list]:
                    tqdm.write("   >> Processing %s using network code %s" % (StationCode, NetworkCode))
                    out = insert_stninfo(NetworkCode, StationCode, stdin)

                    if out:
                        tqdm.write(out)
                else:
                    tqdm.write('   >> Station %s was not found in the station info file %s' % (StationCode, 'standard input'))

    if os.path.isfile(stn_info_path):
        path2stninfo = [stn_info_path]
    else:
        _, path2stninfo = pyArchive.scan_archive_struct_stninfo(stn_info_path)

    print "   >> Found %i Station Info files." % (len(path2stninfo))

    for stninfopath in path2stninfo:

        stn_info_obj = pyStationInfo.StationInfo(cnn)
        stn_list = stn_info_obj.parse_station_info(stninfopath)

        if stn_info_stn.lower() == 'all':
            # parse the station info and get all the stations to go one by one
            print "   >> All stations from station info requested to be added using network code %s" % (NetworkCode)

            for stn in tqdm(stn_list, total=len(stn_list)):
                tqdm.write("     >> Processing %s using network code %s" % (stn['StationCode'].lower(), NetworkCode))
                out = insert_stninfo(NetworkCode, stn['StationCode'].lower(), stninfopath)
                if out:
                    tqdm.write(out)
        else:
            if ',' in stn_info_stn:
                stations = stn_info_stn.lower().split(',')
            else:
                stations = [stn_info_stn.lower()]

            for StationCode in tqdm(stations, total=len(stations)):
                if StationCode in [stn['StationCode'].lower() for stn in stn_list]:
                    tqdm.write("   >> Processing %s using network code %s" % (StationCode, NetworkCode))
                    out = insert_stninfo(NetworkCode,StationCode,stninfopath)

                    if out:
                        tqdm.write(out)
                else:
                    tqdm.write('   >> Station %s was not found in the station info file %s' % (StationCode, stninfopath))

    return

def process_ppp(cnn, pyArchive, archive_path, job_server, run_parallel, Config):

    print " >> Running PPP to the RINEX files in the archive..."

    # for each rinex in the db, run PPP and get a coordinate
    rs_rnx = cnn.query('SELECT rinex.* FROM rinex '
                       'LEFT JOIN ppp_soln ON '
                       'rinex."NetworkCode" = ppp_soln."NetworkCode" AND '
                       'rinex."StationCode" = ppp_soln."StationCode" AND '
                       'rinex."ObservationYear" = ppp_soln."Year" AND '
                       'rinex."ObservationDOY" = ppp_soln."DOY" '
                       'WHERE ppp_soln."NetworkCode" is null '
                       'ORDER BY "ObservationSTime"')

    tblrinex = rs_rnx.dictresult()

    pbar = tqdm(total=len(tblrinex), ncols=80)
    callback = []
    submit = 0
    for record in tblrinex:

        rinex_path = pyArchive.build_rinex_path(record['NetworkCode'], record['StationCode'],
                                                record['ObservationYear'], record['ObservationDOY'])

        # add the base dir
        rinex_path = os.path.join(archive_path, rinex_path)

        if run_parallel:

            callback.append(callback_class(pbar))
            job_server.submit(execute_ppp, args=(record, rinex_path, Config),
                              depfuncs=(remove_from_archive,),
                              modules=('dbConnection', 'pyRinex', 'pyPPP', 'pyStationInfo', 'pyDate', 'pySp3', 'os'),
                              callback=callback[submit].callbackfunc)
            submit += 1

            if submit > 300:
                # when we submit more than 300 jobs, wait until this batch is complete
                job_server.wait()
                # handle any output messages during this batch
                callback = output_handle(callback)
                submit = 0

        else:
            callback.append(callback_class(pbar))
            callback[0].callbackfunc(execute_ppp(record, rinex_path, Config))
            callback = output_handle(callback)

    if run_parallel:
        job_server.wait()

    # handle any output messages during this batch
    output_handle(callback)
    pbar.close()

    if run_parallel:
        print '\n'
        job_server.print_stats()

def print_help():
    print "  usage: "
    print "         --rinex  : scan for rinex"
    print "         --rnxcft : resolve rinex conflicts (multiple files per day)"
    print "         --otl    : calculate OTL parameters for stations in the database"
    print "         --stninfo: scan for station info files in the archive"
    print "                    if no arguments, searches the archive for station info files and uses their location to determine network"
    print "                    else, use: --stninfo_path --stn --network, where"
    print "                    --stninfo_path: path to a dir with station info files, or single station info file. Leave empy to use stdin"
    print "                    --stn         : station to search for in the station info, of list of stations separated by comma, no spaces between ('all' will try to add all of them)"
    print "                    --net         : network name that has to be used to add the station information"
    print "         --ppp    : run ppp to the rinex files in the archive"
    print "         --all    : do all of the above"


def main(argv):

    run_stninfo = False
    run_otl = False
    run_rinex = False
    run_ppp = False
    run_conflicts = False
    stn_info_path = None
    stn_info_stn = None
    stn_info_net = None
    stn_info_stdin = None

    if not argv:
        print "Scan the archive using configuration file gnss_data.cfg"
        print_help()
        exit()

    try:
        aoptions, arguments = getopt.getopt(argv,'',['rinex', 'rnxcft', 'otl', 'stninfo', 'ppp', 'all', 'stninfo_path=', 'stn=', 'net=', 'noparallel'])
    except getopt.GetoptError:
        print "invalid argument/s"
        print_help()
        sys.exit(2)

    Config = pyOptions.ReadOptions("gnss_data.cfg") # type: pyOptions.ReadOptions

    for opt, args in aoptions:
        if opt == '--stninfo':
            run_stninfo = True
        if opt == '--stninfo_path':
            stn_info_path = args
        if opt == '--stn':
            stn_info_stn = args
        if opt == '--net':
            stn_info_net = args
        elif opt == '--otl':
            run_otl = True
        elif opt == '--rinex':
            run_rinex = True
        elif opt == '--ppp':
            run_ppp = True
        elif opt == '--noparallel':
            Config.run_parallel = False
        elif opt == '--all':
            run_stninfo = True
            run_conflicts = True
            run_otl = True
            run_rinex = True
            run_ppp = True

    if not stn_info_path and run_stninfo:
        print 'No --stn_info_path: reading from stdin'
        stn_info_stdin = []
        for line in sys.stdin:
            stn_info_stdin.append(line)
        stn_info_path = 'stdin?'

    if (stn_info_stn or stn_info_net) and not run_stninfo:
        print "invalid arguments without --stninfo"
        print_help()
        exit(2)

    cnn = dbConnection.Cnn("gnss_data.cfg")

    pyArchive = pyArchiveStruct.RinexStruct(cnn)

    #################################
    # configure the parallel python if run in parallel = true

    if Config.run_parallel:
        ppservers = ('*',)
        job_server = pp.Server(ncpus=Utils.get_processor_count(), ppservers=ppservers)
        time.sleep(1)
        print "Starting pp with", job_server.get_active_nodes(), "workers"
    else:
        job_server = None

    #########################################

    if run_rinex:
        scan_rinex(cnn, job_server, pyArchive, Config.archive_path, Config)

    if run_conflicts:
        process_conflicts(cnn, pyArchive, Config.archive_path)

    #########################################

    if run_otl:
        process_otl(cnn, job_server, Config.run_parallel, Config.archive_path, Config.brdc_path, Config.options, Config.sp3types, Config.sp3altrn)

    #########################################

    if run_stninfo:
        if stn_info_path is None:
            scan_station_info(job_server, Config.run_parallel, pyArchive, Config.archive_path)
        else:
            scan_station_info_manual(cnn, pyArchive, stn_info_path, stn_info_stn, stn_info_net, stn_info_stdin)

    #########################################

    if run_ppp:
        process_ppp(cnn, pyArchive, Config.archive_path, job_server, Config.run_parallel, Config)

    #########################################

    # remove the production dir
    #if os.path.isdir('production'):
    #    rmtree('production')

if __name__ == '__main__':

    main(sys.argv[1:])