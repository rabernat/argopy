"""
Argo file index store

This file has the prototype and 2 implementations, one based on pyarrow and the other based on pandas
"""

import numpy as np
import pandas as pd
import logging
from abc import ABC, abstractmethod
from fsspec.core import split_protocol
import io
import gzip

from argopy.options import OPTIONS
from argopy.stores import httpstore, memorystore, filestore, ftpstore
from argopy.errors import DataNotFound, FtpPathError, InvalidDataset
from argopy.utilities import check_index_cols, is_indexbox, check_wmo, check_cyc

try:
    import pyarrow.csv as csv
    import pyarrow as pa
    # import pyarrow.parquet as pq
except ModuleNotFoundError:
    pass


log = logging.getLogger("argopy.stores.index")


class ArgoIndexStoreProto(ABC):
    """ Prototype for Argo index store

        API Design:
        -----------
        An index store is instantiated with the access path (host) and the index file:
        >>> idx = indexstore()
        >>> idx = indexstore(host="ftp://ftp.ifremer.fr/ifremer/argo")
        >>> idx = indexstore(host="https://data-argo.ifremer.fr", index_file="ar_index_global_prof.txt")
        >>> idx = indexstore(host="https://data-argo.ifremer.fr", index_file="ar_index_global_prof.txt", cache=True)

        Index methods and properties:
        >>> idx.load()
        >>> idx.load(nrows=12)  # Only load the first N rows of the index
        >>> idx.N_RECORDS  # Shortcut for length of 1st dimension of the index array
        >>> idx.index  # internal storage structure of the full index (:class:`pyarrow.Table` or :class:`pandas.DataFrame`)
        >>> idx.shape  # shape of the full index array
        >>> idx.uri_full_index  # List of absolute path to files from the full index table column 'file'
        >>> idx.to_dataframe(index=True)  # Convert index to user-friendly :class:`pandas.DataFrame`
        >>> idx.to_dataframe(index=True, nrows=2)  # Only returns the first nrows of the index

        Search methods and properties:
        >>> idx.search_wmo(1901393)
        >>> idx.search_wmo_cyc(1901393, [1,12])
        >>> idx.search_tim([-60, -55, 40., 45., '2007-08-01', '2007-09-01'])  # Take an index BOX definition
        >>> idx.search_lat_lon([-60, -55, 40., 45., '2007-08-01', '2007-09-01'])  # Take an index BOX definition
        >>> idx.search_lat_lon_tim([-60, -55, 40., 45., '2007-08-01', '2007-09-01'])  # Take an index BOX definition
        >>> idx.N_MATCH  # Shortcut for length of 1st dimension of the search results array
        >>> idx.search  # Pyarrow table with search results
        >>> idx.uri  # List of absolute path to files from the search results table column 'file'
        >>> idx.run()  # Run the search and save results in cache if necessary
        >>> idx.to_dataframe()  # Convert search results to user-friendly :class:`pandas.DataFrame`
        >>> idx.to_dataframe(nrows=2)  # Only returns the first nrows of the search results


        Misc:
        >>> idx.cname
        >>> idx.read_wmo
        >>> idx.records_per_wmo

    """
    backend = '?'
    search_type = '?'

    def __init__(self,
                 host: str = "https://data-argo.ifremer.fr",
                 index_file: str = "ar_index_global_prof.txt",
                 cache: bool = False,
                 cachedir: str = "",
                 timeout: int = 0):
        """ Create an Argo index file store

            Parameters
            ----------
            host: str, default: "https://data-argo.ifremer.fr"
                Host is a local or remote ftp/http path to a 'dac' folder (GDAC structure compliant). This takes values
                like: "ftp://ftp.ifremer.fr/ifremer/argo", "ftp://usgodae.org/pub/outgoing/argo" or a local absolute path.
            index_file: str, default: "ar_index_global_prof.txt"
            cache : bool (False)
            cachedir : str (used value in global OPTIONS)
        """
        self.host = host
        self.index_file = index_file
        self.cache = cache
        self.cachedir = OPTIONS['cachedir'] if cachedir == '' else cachedir
        timeout = OPTIONS["api_timeout"] if timeout == 0 else timeout
        self.fs = {}
        if split_protocol(host)[0] is None:
            self.fs['index'] = filestore(cache=cache, cachedir=cachedir)
        elif 'https' in split_protocol(host)[0]:
            # Only for https://data-argo.ifremer.fr (much faster than the ftp servers)
            self.fs['index'] = httpstore(cache=cache, cachedir=cachedir, timeout=timeout, size_policy='head')
        elif 'ftp' in split_protocol(host)[0]:
            if 'ifremer' not in host and 'usgodae' not in host:
                raise FtpPathError("Unknown Argo ftp: %s" % host)
            self.fs['index'] = ftpstore(host=split_protocol(host)[-1].split('/')[0],  # eg: ftp.ifremer.fr
                                        cache=cache,
                                        cachedir=cachedir,
                                        timeout=timeout,
                                        block_size=1000*(2**20))
        else:
            raise FtpPathError("Unknown protocol for an Argo index store: %s" % split_protocol(host)[0])
        self.fs['search'] = memorystore(cache, cachedir)  # Manage search results

        self.index_path = self.fs['index'].fs.sep.join([self.host, self.index_file])
        if not self.fs['index'].exists(self.index_path):
            raise FtpPathError("Index file does not exist: %s" % self.index_path)

    def __repr__(self):
        summary = ["<argoindex.%s>" % self.backend]
        summary.append("Host: %s" % self.host)
        summary.append("Index: %s" % self.index_file)
        if hasattr(self, 'index'):
            summary.append("Loaded: True (%i records)" % self.N_RECORDS)
        else:
            summary.append("Loaded: False")
        if hasattr(self, 'search'):
            match = 'matches' if self.N_MATCH > 1 else 'match'
            summary.append("Searched: True (%i %s, %0.4f%%)" % (self.N_MATCH, match, self.N_MATCH * 100 / self.N_RECORDS))
        else:
            summary.append("Searched: False")
        return "\n".join(summary)

    def _format(self, x, typ: str) -> str:
        """ string formatting helper """
        if typ == "lon":
            if x < 0:
                x = 360.0 + x
            return ("%05d") % (x * 100.0)
        if typ == "lat":
            return ("%05d") % (x * 100.0)
        if typ == "prs":
            return ("%05d") % (np.abs(x) * 10.0)
        if typ == "tim":
            return pd.to_datetime(x).strftime("%Y-%m-%d")
        return str(x)

    @property
    def cname(self) -> str:
        """ Return the search constraint(s) as a formatted string

            Return 'full' if a search was not performed on the indexstore instance

            This method uses the BOX, WMO, CYC keys of the index instance ``search_type`` property
         """
        cname = "full"

        if "BOX" in self.search_type:
            BOX = self.search_type['BOX']
            cname = ("x=%0.2f/%0.2f;y=%0.2f/%0.2f") % (
                BOX[0],
                BOX[1],
                BOX[2],
                BOX[3],
            )
            if len(BOX) == 6:
                cname = ("x=%0.2f/%0.2f;y=%0.2f/%0.2f;t=%s/%s") % (
                    BOX[0],
                    BOX[1],
                    BOX[2],
                    BOX[3],
                    self._format(BOX[4], "tim"),
                    self._format(BOX[5], "tim"),
                )

        elif "WMO" in self.search_type:
            WMO = self.search_type['WMO']
            if "CYC" in self.search_type:
                CYC = self.search_type['CYC']

            prtcyc = lambda CYC, wmo: "WMO%i_%s" % (  # noqa: E731
                wmo,
                "_".join(["CYC%i" % (cyc) for cyc in sorted(CYC)]),
            )

            if len(WMO) == 1:
                if "CYC" in self.search_type:
                    cname = "%s" % prtcyc(CYC, WMO[0])
                else:
                    cname = "WMO%i" % (WMO[0])
            else:
                cname = ";".join(["WMO%i" % wmo for wmo in sorted(WMO)])
                if "CYC" in self.search_type:
                    cname = ";".join([prtcyc(CYC, wmo) for wmo in WMO])
                cname = "%s" % cname

        elif "CYC" in self.search_type and "WMO" not in self.search_type:
            CYC = self.search_type['CYC']
            if len(CYC) == 1:
                cname = "CYC%i" % (CYC[0])
            else:
                cname = ";".join(["CYC%i" % cyc for cyc in sorted(CYC)])
            cname = "%s" % cname

        return cname

    def _sha_from(self, path):
        """ Internal post-processing for a sha

            Used by: sha_df, sha_pq, sha_h5
        """
        sha = path  # no encoding
        # sha = hashlib.sha256(path.encode()).hexdigest()  # Full encoding
        # log.debug("%s > %s" % (path, sha))
        return sha

    @property
    def sha_df(self) -> str:
        """ Returns a unique SHA for a cname/dataframe """
        cname = "pd-%s" % self.cname
        sha = self._sha_from(cname)
        return sha

    @property
    def sha_pq(self) -> str:
        """ Returns a unique SHA for a cname/parquet """
        cname = "pq-%s" % self.cname
        # if cname == "full":
        #     raise ValueError("Search not initialised")
        # else:
        #     path = cname
        sha = self._sha_from(cname)
        return sha

    @property
    def sha_h5(self) -> str:
        """ Returns a unique SHA for a cname/hdf5 """
        cname = "h5-%s" % self.cname
        # if cname == "full":
        #     raise ValueError("Search not initialised")
        # else:
        #     path = cname
        sha = self._sha_from(cname)
        return sha

    @property
    def shape(self):
        """ Shape of the index array """
        # Must work for all internal storage type (:class:`pyarrow.Table` or :class:`pandas.DataFrame`)
        return self.index.shape

    @property
    def N_FILES(self):
        """ Number of rows in search result or index if search not triggered """
        # Must work for all internal storage type (:class:`pyarrow.Table` or :class:`pandas.DataFrame`)
        if hasattr(self, 'search'):
            return self.search.shape[0]
        elif hasattr(self, 'index'):
            return self.index.shape[0]
        else:
            raise InvalidDataset("You must, at least, load the index first !")

    @property
    def N_RECORDS(self):
        """ Number of rows in the full index """
        # Must work for all internal storage type (:class:`pyarrow.Table` or :class:`pandas.DataFrame`)
        if hasattr(self, 'index'):
            return self.index.shape[0]
        else:
            raise InvalidDataset("Load the index first !")

    @property
    def N_MATCH(self):
        """ Number of rows in search result """
        # Must work for all internal storage type (:class:`pyarrow.Table` or :class:`pandas.DataFrame`)
        if hasattr(self, 'search'):
            return self.search.shape[0]
        else:
            raise InvalidDataset("Initialised search first !")

    def _write(self, fs, path, obj, format='pq'):
        """ Write internal array object to file store

            Parameters
            ----------
            obj: :class:`pyarrow.Table` or :class:`pandas.DataFrame`
        """
        with fs.open(path, "wb") as handle:
            if format in ['pq', 'parquet']:
                pa.parquet.write_table(obj, handle)
            elif format in ['pd']:
                obj.to_pickle(handle)  # obj is a pandas dataframe
        return self

    def _read(self, fs, path, format='pq'):
        """ Read internal array object from file store

            Returns
            -------
            obj: :class:`pyarrow.Table` or :class:`pandas.DataFrame`
        """
        with fs.open(path, "rb") as handle:
            if format in ['pq', 'parquet']:
                obj = pa.parquet.read_table(handle)
            elif format in ['pd']:
                obj = pd.read_pickle(handle)
        return obj

    def clear_cache(self):
        self.fs['index'].clear_cache()
        self.fs['search'].clear_cache()
        return self

    @property
    @abstractmethod
    def search_path(self):
        """ Path to search result uri

        Returns
        -------
        str
        """
        raise NotImplementedError("Not implemented")

    @property
    @abstractmethod
    def uri_full_index(self):
        """ List of URI from index

        Returns
        -------
        list(str)
        """
        raise NotImplementedError("Not implemented")

    @property
    @abstractmethod
    def uri(self):
        """ List of URI from search results

        Returns
        -------
        list(str)
        """
        raise NotImplementedError("Not implemented")

    @abstractmethod
    def load(self, force=False):
        """ Load an Argo-index file content

        Fill in self.index internal property
        If store is cached, caching is triggered here
        """
        raise NotImplementedError("Not implemented")

    @abstractmethod
    def run(self):
        """ Filter index with search criteria

        Fill in self.search internal property
        If store is cached, caching is triggered here
        """
        raise NotImplementedError("Not implemented")

    @abstractmethod
    def to_dataframe(self) -> pd.DataFrame:
        """ Return search results as dataframe

        If store is cached, caching is triggered here
        """
        raise NotImplementedError("Not implemented")

    @abstractmethod
    def read_wmo(self):
        """ Return list of unique WMOs in search results

        Fall back on full index if search not found

        Returns
        -------
        list(int)
        """
        raise NotImplementedError("Not implemented")

    @abstractmethod
    def records_per_wmo(self):
        """ Return the number of records per unique WMOs in search results

        Fall back on full index if search not found

        Returns
        -------
        dict
            WMO are in keys, nb of records in values
        """
        raise NotImplementedError("Not implemented")

    @abstractmethod
    def search_wmo(self, WMOs):
        """ Search index for WMO

        - Define search method
        - Trigger self.run() to set self.search internal property

        Parameters
        ----------
        list(int)
            List of WMOs to search
        """
        raise NotImplementedError("Not implemented")

    @abstractmethod
    def search_wmo_cyc(self, WMOs, CYCs):
        """ Search index for WMO and CYC

        - Define search method
        - Trigger self.run() to set self.search internal property

        Parameters
        ----------
        list(int)
            List of WMOs to search
        list(int)
            List of CYCs to search
        """
        raise NotImplementedError("Not implemented")

    @abstractmethod
    def search_tim(self, BOX):
        """ Search index for time range

        - Define search method
        - Trigger self.run() to set self.search internal property

        Parameters
        ----------
        box : list()
            An index box to search Argo records for:

                - box = [lon_min, lon_max, lat_min, lat_max, datim_min, datim_max]
        """
        raise NotImplementedError("Not implemented")

    @abstractmethod
    def search_lat_lon(self, BOX):
        """ Search index for rectangular latitude/longitude domain

        - Define search method
        - Trigger self.run() to set self.search internal property

        Parameters
        ----------
        box : list()
            An index box to search Argo records for:

                - box = [lon_min, lon_max, lat_min, lat_max, datim_min, datim_max]
        """
        raise NotImplementedError("Not implemented")

    @abstractmethod
    def search_lat_lon_tim(self, BOX):
        """ Search index for rectangular latitude/longitude domain and time range

        - Define search method
        - Trigger self.run() to set self.search internal property

        Parameters
        ----------
        box : list()
            An index box to search Argo records for:

                - box = [lon_min, lon_max, lat_min, lat_max, datim_min, datim_max]
        """
        raise NotImplementedError("Not implemented")


class indexstore_pyarrow(ArgoIndexStoreProto):
    """ Argo index store, using :class:`pyarrow.Table` as internal storage format """
    backend = 'pyarrow'

    def load(self, force=False, nrows=None):
        """ Load an Argo-index file content

        Try to load the gzipped file first, and if not found, fall back on the raw .txt file.

        Returns
        -------
        pyarrow.table
        """

        def read_csv(input_file, nrows=None):
            # pyarrow doesn't have a concept of 'nrows' but it's really important
            # for partial downloading of the giant prof index
            # This is totaly copied from: https://github.com/ArgoCanada/argopandas/blob/master/argopandas/global_index.py#L20
            if nrows is not None:
                buf = io.BytesIO()
                n = 0
                for line in input_file:
                    n += 1
                    buf.write(line)
                    if n >= (nrows + 8 + 1):
                        break

                buf.seek(0)
                return read_csv(buf, nrows=None)

            this_table = csv.read_csv(
                input_file,
                read_options=csv.ReadOptions(use_threads=True, skip_rows=8),
                convert_options=csv.ConvertOptions(
                    column_types={
                        'date': pa.timestamp('s', tz='utc'),
                        'date_update': pa.timestamp('s', tz='utc')
                    },
                    timestamp_parsers=['%Y%m%d%H%M%S']
                )
            )
            return this_table

        if not hasattr(self, 'index') or force:
            this_path = self.index_path
            if self.fs['index'].exists(this_path + '.gz'):
                with self.fs['index'].open(this_path + '.gz', "rb") as fg:
                    with gzip.open(fg) as f:
                        self.index = read_csv(f, nrows=nrows)
                        check_index_cols(self.index.column_names, convention=self.index_file.split(".")[0])
                        log.debug("Argo index file loaded with pyarrow read_csv. src='%s'" % (this_path + '.gz'))
            else:
                with self.fs['index'].open(this_path, "rb") as f:
                    self.index = read_csv(f, nrows=nrows)
                    check_index_cols(self.index.column_names, convention=self.index_file.split(".")[0])
                    log.debug("Argo index file loaded with pyarrow read_csv. src='%s'" % this_path)

        if self.N_RECORDS == 0:
            raise DataNotFound("No data found in the index")
        elif nrows is not None and self.N_RECORDS != nrows:
            self.index = self.index[0:nrows-1]

        return self

    def run(self, nrows=None):
        """ Filter index with search criteria """
        if self.cache and self.fs['search'].exists(self.search_path):
            log.debug("Search results already in memory as pyarrow table, loading... src='%s'" % (self.search_path))
            self.search = self._read(self.fs['search'].fs, self.search_path)
        else:
            log.debug('Compute search from scratch ...')
            this_filter = np.nonzero(self.search_filter)[0]
            n_match = this_filter.shape[0]
            if nrows is not None and n_match > 0:
                self.search = self.index.take(this_filter.take(range(np.min([nrows, n_match]))))
            else:
                self.search = self.index.filter(self.search_filter)

            log.debug('Found %i matches' % self.search.shape[0])
            if self.cache and self.search.shape[0] > 0:
                self._write(self.fs['search'], self.search_path, self.search)
                self.search = self._read(self.fs['search'].fs, self.search_path)
                log.debug("Search results saved in cache as pyarrow table. dest='%s'" % self.search_path)
        return self

    def to_dataframe(self, nrows=None, index=False):  # noqa: C901
        """ Return index or search results as pandas dataframe

            If search not triggered, fall back on full index by default. Using index=True force to return the full index.

            This is where we can process the internal dataframe structure for the end user.
            If this processing is long, we can implement caching here.
        """
        if hasattr(self, 'search') and not index:
            this_path = self.host + "/" + self.index_file + "/" + self.sha_pq
            src = 'search results'
            if self.N_MATCH == 0:
                raise DataNotFound("No data found in the index corresponding to your search criteria."
                                   " Search definition: %s" % self.cname)
            else:
                df = self.search.to_pandas()
        else:
            this_path = self.host + "/" + self.index_file + "/" + self._sha_from("pq-full")
            src = 'full index'
            if not hasattr(self, 'index'):
                self.load(nrows=nrows)
            df = self.index.to_pandas()

        if nrows is not None:
            this_path = this_path + "/export" + ".%i" % nrows
        else:
            this_path = this_path + ".export"

        if self.cache and self.fs['search'].exists(this_path):
            log.debug("[%s] already processed as Dataframe, loading ... src='%s'" % (src, this_path))
            df = self._read(self.fs['search'].fs, this_path, format='pd')
        else:
            log.debug("Converting [%s] to dataframe from scratch ..." % src)
            # Post-processing for user:
            from argopy.utilities import load_dict, mapp_dict

            if nrows is not None:
                df = df.loc[0:nrows - 1].copy()

            if 'index' in df:
                df.drop('index', axis=1, inplace=True)

            df.reset_index(drop=True, inplace=True)

            df['wmo'] = df['file'].apply(lambda x: int(x.split('/')[1]))
            df['date'] = pd.to_datetime(df['date'], format="%Y%m%d%H%M%S")
            df['date_update'] = pd.to_datetime(df['date_update'], format="%Y%m%d%H%M%S")

            # institution & profiler mapping for all users
            # todo: may be we need to separate this for standard and expert users
            institution_dictionnary = load_dict('institutions')
            df['tmp1'] = df['institution'].apply(lambda x: mapp_dict(institution_dictionnary, x))
            df = df.rename(columns={"institution": "institution_code", "tmp1": "institution"})

            profiler_dictionnary = load_dict('profilers')
            profiler_dictionnary['?'] = '?'

            def ev(x):
                try:
                    return int(x)
                except Exception:
                    return x
            df['profiler'] = df['profiler_type'].apply(lambda x: mapp_dict(profiler_dictionnary, ev(x)))
            df = df.rename(columns={"profiler_type": "profiler_code"})

            if self.cache:
                self._write(self.fs['search'], this_path, df, format='pd')
                df = self._read(self.fs['search'].fs, this_path, format='pd')
                log.debug("This dataframe saved in cache. dest='%s'" % this_path)

        return df

    @property
    def search_path(self):
        """ Path to search result uri"""
        return self.host + "/" + self.index_file + "." + self.sha_pq

    @property
    def uri_full_index(self):
        return ["/".join([self.host, "dac", f.as_py()]) for f in self.index['file']]

    @property
    def uri(self):
        return ["/".join([self.host, "dac", f.as_py()]) for f in self.search['file']]

    def read_wmo(self, index=False):
        """ Return list of unique WMOs in search results

        Fall back on full index if search not found

        Returns
        -------
        list(int)
        """
        if hasattr(self, 'search') and not index:
            results = pa.compute.split_pattern(self.search['file'], pattern="/")
        else:
            results = pa.compute.split_pattern(self.index['file'], pattern="/")
        df = results.to_pandas()

        def fct(row):
            return row[1]
        wmo = df.map(fct)
        wmo = wmo.unique()
        wmo = [int(w) for w in wmo]
        return wmo

    def records_per_wmo(self, index=False):
        """ Return the number of records per unique WMOs in search results

            Fall back on full index if search not found
        """
        ulist = self.read_wmo()
        count = {}
        for wmo in ulist:
            if hasattr(self, 'search') and not index:
                search_filter = pa.compute.match_substring_regex(self.search['file'], pattern="/%i/" % wmo)
                count[wmo] = self.search.filter(search_filter).shape[0]
            else:
                search_filter = pa.compute.match_substring_regex(self.index['file'], pattern="/%i/" % wmo)
                count[wmo] = self.index.filter(search_filter).shape[0]
        return count

    def search_wmo(self, WMOs, nrows=None):
        WMOs = check_wmo(WMOs)  # Check and return a valid list of WMOs
        log.debug("Argo index searching for WMOs=[%s] ..." % ";".join([str(wmo) for wmo in WMOs]))
        self.load()
        self.search_type = {'WMO': WMOs}
        filt = []
        for wmo in WMOs:
            filt.append(pa.compute.match_substring_regex(self.index['file'], pattern="/%i/" % wmo))
        self.search_filter = np.logical_or.reduce(filt)
        self.run(nrows=nrows)
        return self

    def search_cyc(self, CYCs, nrows=None):
        CYCs = check_cyc(CYCs)  # Check and return a valid list of CYCs
        log.debug("Argo index searching for CYCs=[%s] ..." % (
            ";".join([str(cyc) for cyc in CYCs])))
        self.load()
        self.search_type = {'CYC': CYCs}
        filt = []
        for cyc in CYCs:
            if cyc < 1000:
                pattern = "_%0.3d.nc" % (cyc)
            else:
                pattern = "_%0.4d.nc" % (cyc)
            filt.append(pa.compute.match_substring_regex(self.index['file'], pattern=pattern))
        self.search_filter = np.logical_or.reduce(filt)
        self.run(nrows=nrows)
        return self

    def search_wmo_cyc(self, WMOs, CYCs, nrows=None):
        WMOs = check_wmo(WMOs)  # Check and return a valid list of WMOs
        CYCs = check_cyc(CYCs)  # Check and return a valid list of CYCs
        log.debug("Argo index searching for WMOs=[%s] and CYCs=[%s] ..." % (
            ";".join([str(wmo) for wmo in WMOs]),
            ";".join([str(cyc) for cyc in CYCs])))
        self.load()
        self.search_type = {'WMO': WMOs, 'CYC': CYCs}
        filt = []
        for wmo in WMOs:
            for cyc in CYCs:
                if cyc < 1000:
                    pattern = "%i_%0.3d.nc" % (wmo, cyc)
                else:
                    pattern = "%i_%0.4d.nc" % (wmo, cyc)
                filt.append(pa.compute.match_substring_regex(self.index['file'], pattern=pattern))
        self.search_filter = np.logical_or.reduce(filt)
        self.run(nrows=nrows)
        return self

    def search_tim(self, BOX, nrows=None):
        is_indexbox(BOX)
        log.debug("Argo index searching for time in BOX=%s ..." % BOX)
        self.load()
        self.search_type = {'BOX': BOX}
        filt = []
        filt.append(pa.compute.greater_equal(pa.compute.cast(self.index['date'], pa.timestamp('ms')),
                                             pa.array([pd.to_datetime(BOX[4])], pa.timestamp('ms'))[0]))
        filt.append(pa.compute.less_equal(pa.compute.cast(self.index['date'], pa.timestamp('ms')),
                                          pa.array([pd.to_datetime(BOX[5])], pa.timestamp('ms'))[0]))
        self.search_filter = np.logical_and.reduce(filt)
        self.run(nrows=nrows)
        return self

    def search_lat_lon(self, BOX, nrows=None):
        is_indexbox(BOX)
        log.debug("Argo index searching for lat/lon in BOX=%s ..." % BOX)
        self.load()
        self.search_type = {'BOX': BOX}
        filt = []
        filt.append(pa.compute.greater_equal(self.index['longitude'], BOX[0]))
        filt.append(pa.compute.less_equal(self.index['longitude'], BOX[1]))
        filt.append(pa.compute.greater_equal(self.index['latitude'], BOX[2]))
        filt.append(pa.compute.less_equal(self.index['latitude'], BOX[3]))
        self.search_filter = np.logical_and.reduce(filt)
        self.run(nrows=nrows)
        return self

    def search_lat_lon_tim(self, BOX, nrows=None):
        is_indexbox(BOX)
        log.debug("Argo index searching for lat/lon/time in BOX=%s ..." % BOX)
        self.load()
        self.search_type = {'BOX': BOX}
        filt = []
        filt.append(pa.compute.greater_equal(self.index['longitude'], BOX[0]))
        filt.append(pa.compute.less_equal(self.index['longitude'], BOX[1]))
        filt.append(pa.compute.greater_equal(self.index['latitude'], BOX[2]))
        filt.append(pa.compute.less_equal(self.index['latitude'], BOX[3]))
        filt.append(pa.compute.greater_equal(pa.compute.cast(self.index['date'], pa.timestamp('ms')),
                                             pa.array([pd.to_datetime(BOX[4])], pa.timestamp('ms'))[0]))
        filt.append(pa.compute.less_equal(pa.compute.cast(self.index['date'], pa.timestamp('ms')),
                                          pa.array([pd.to_datetime(BOX[5])], pa.timestamp('ms'))[0]))
        self.search_filter = np.logical_and.reduce(filt)
        self.run(nrows=nrows)
        return self


class indexstore_pandas(ArgoIndexStoreProto):
    """ Argo index store, using :class:`pandas.DataFrame` as internal storage format """
    backend = 'pandas'

    def load(self, force=False, nrows=None):
        """ Load an Argo-index file content

        Try to load the gzipped file first, and if not found, fall back on the raw .txt file.

        Returns
        -------
        pandas.dataframe
        """

        def read_csv(input_file, nrows=None):
            this_table = pd.read_csv(input_file, sep=',', index_col=None, header=0, skiprows=8, nrows=nrows)
            return this_table

        if not hasattr(self, 'index') or force:
            index_path = self.host + "/" + self.index_file

            if self.fs['index'].exists(index_path + '.gz'):
                with self.fs['index'].open(index_path + '.gz', "rb") as fg:
                    with gzip.open(fg) as f:
                        df = read_csv(f, nrows=nrows)
                        check_index_cols(df.columns.to_list(), convention=self.index_file.split(".")[0])
                        self.index = df
                        log.debug("Argo index file loaded with pandas read_csv. src='%s'" % (index_path + '.gz'))

            else:
                with self.fs['index'].open(index_path, "rb") as f:
                    df = read_csv(f, nrows=nrows)
                    check_index_cols(df.columns.to_list(), convention=self.index_file.split(".")[0])
                    self.index = df
                    log.debug("Argo index file loaded with pandas read_csv. src='%s'" % index_path)

        if self.N_RECORDS == 0:
            raise DataNotFound("No data found in the index")
        elif nrows is not None and self.N_RECORDS != nrows:
            self.index = self.index[0:nrows-1]

        return self

    @property
    def search_path(self):
        """ Path to search result uri"""
        return self.host + "/" + self.index_file + "." + self.sha_df

    @property
    def uri_full_index(self):
        return ["/".join([self.host, "dac", f]) for f in self.index['file']]

    @property
    def uri(self):
        return ["/".join([self.host, "dac", f]) for f in self.search['file']]

    def read_wmo(self):
        """ Return list of unique WMOs in search results

        Fall back on full index if search not found

        Returns
        -------
        list(int)
        """
        if hasattr(self, 'search'):
            results = self.search['file'].apply(lambda x: int(x.split('/')[1]))
        else:
            results = self.index['file'].apply(lambda x: int(x.split('/')[1]))
        wmo = np.unique(results)
        return wmo

    def records_per_wmo(self):
        """ Return the number of records per unique WMOs in search results

            Fall back on full index if search not found

        Returns
        -------
        dict
        """
        ulist = self.read_wmo()
        count = {}
        for wmo in ulist:
            if hasattr(self, 'search'):
                search_filter = self.search['file'].str.contains("/%i/" % wmo, regex=True, case=False)
                count[wmo] = self.search[search_filter].shape[0]
            else:
                search_filter = self.index['file'].str.contains("/%i/" % wmo, regex=True, case=False)
                count[wmo] = self.index[search_filter].shape[0]
        return count

    def run(self, nrows=None):
        """ Filter index with search criteria """
        if self.cache and self.fs['search'].exists(self.search_path):
            log.debug("Search results already in memory as pandas dataframe, loading... src='%s'" % (self.search_path))
            self.search = self._read(self.fs['search'].fs, self.search_path, format='pd')
        else:
            log.debug("Compute search from scratch ... (not found '%s')" % self.search_path)
            this_filter = np.nonzero(self.search_filter)[0]
            n_match = this_filter.shape[0]
            if nrows is not None and n_match > 0:
                self.search = self.index.head(np.min([nrows, n_match])).reset_index()
            else:
                self.search = self.index[self.search_filter].reset_index()
            log.debug('Found %i matches' % self.search.shape[0])
            if self.cache and self.search.shape[0] > 0:
                self._write(self.fs['search'], self.search_path, self.search, format='pd')
                self.search = self._read(self.fs['search'].fs, self.search_path, format='pd')
                log.debug("Search results saved in cache as pandas dataframe. dest='%s'" % self.search_path)
        return self

    def to_dataframe(self, nrows=None, index=False):  # noqa: C901
        """ Return search results as dataframe

            This is where we can process the internal dataframe structure for the end user.
            If this processing is long, we can implement caching here.
        """
        if hasattr(self, 'search') and not index:
            this_path = self.host + "/" + self.index_file + "/" + self.sha_df
            src = 'search results'
            if self.N_MATCH == 0:
                raise DataNotFound("No data found in the index corresponding to your search criteria."
                                   " Search definition: %s" % self.cname)
            else:
                df = self.search.copy()
        else:
            this_path = self.host + "/" + self.index_file + "/" + self._sha_from("pd-full")
            src = 'full index'
            if not hasattr(self, 'index'):
                self.load(nrows=nrows)
            df = self.index.copy()

        if nrows is not None:
            this_path = this_path + "/export" + ".%i" % nrows
        else:
            this_path = this_path + ".export"

        if self.cache and self.fs['search'].exists(this_path):
            log.debug("[%s] already processed as Dataframe, loading ... src='%s'" % (src, this_path))
            df = self._read(self.fs['search'].fs, this_path, format='pd')
        else:
            log.debug("Converting [%s] to dataframe from scratch ..." % src)
            # log.debug("Cache: %s" % self.cache)
            # log.debug("this_path: %s" % this_path)
            # log.debug("exists: %s" % self.fs['search'].exists(this_path))

            # Post-processing for user:
            from argopy.utilities import load_dict, mapp_dict

            if nrows is not None:
                df = df.loc[0:nrows - 1].copy()

            if 'index' in df:
                df.drop('index', axis=1, inplace=True)

            df.reset_index(drop=True, inplace=True)

            df['wmo'] = df['file'].apply(lambda x: int(x.split('/')[1]))
            df['date'] = pd.to_datetime(df['date'], format="%Y%m%d%H%M%S")
            df['date_update'] = pd.to_datetime(df['date_update'], format="%Y%m%d%H%M%S")

            # institution & profiler mapping for all users
            # todo: may be we need to separate this for standard and expert users
            institution_dictionnary = load_dict('institutions')
            df['tmp1'] = df['institution'].apply(lambda x: mapp_dict(institution_dictionnary, x))
            df = df.rename(columns={"institution": "institution_code", "tmp1": "institution"})

            profiler_dictionnary = load_dict('profilers')
            profiler_dictionnary['?'] = '?'

            def ev(x):
                try:
                    return int(x)
                except Exception:
                    return x

            df['profiler'] = df['profiler_type'].apply(lambda x: mapp_dict(profiler_dictionnary, ev(x)))
            df = df.rename(columns={"profiler_type": "profiler_code"})

            if self.cache:
                # log.debug("Saving this dataframe to cache. dest='%s'" % this_path)
                self._write(self.fs['search'], this_path, df, format='pd')
                df = self._read(self.fs['search'].fs, this_path, format='pd')
                log.debug("This dataframe saved in cache. dest='%s'" % this_path)

        return df

    def search_wmo(self, WMOs, nrows=None):
        WMOs = check_wmo(WMOs)  # Check and return a valid list of WMOs
        log.debug("Argo index searching for WMOs=[%s] ..." % ";".join([str(wmo) for wmo in WMOs]))
        self.load()
        self.search_type = {'WMO': WMOs}
        filt = []
        for wmo in WMOs:
            filt.append(self.index['file'].str.contains("/%i/" % wmo, regex=True, case=False))
        self.search_filter = np.logical_or.reduce(filt)
        self.run(nrows=nrows)
        return self

    def search_cyc(self, CYCs, nrows=None):
        CYCs = check_cyc(CYCs)  # Check and return a valid list of CYCs
        log.debug("Argo index searching for CYCs=[%s] ..." % (
            ";".join([str(cyc) for cyc in CYCs])))
        self.load()
        self.search_type = {'CYC': CYCs}
        filt = []
        for cyc in CYCs:
            if cyc < 1000:
                pattern = "_%0.3d.nc" % (cyc)
            else:
                pattern = "_%0.4d.nc" % (cyc)
            filt.append(self.index['file'].str.contains(pattern, regex=True, case=False))
        self.search_filter = np.logical_or.reduce(filt)
        self.run(nrows=nrows)
        return self

    def search_wmo_cyc(self, WMOs, CYCs, nrows=None):
        WMOs = check_wmo(WMOs)  # Check and return a valid list of WMOs
        CYCs = check_cyc(CYCs)  # Check and return a valid list of CYCs
        log.debug("Argo index searching for WMOs=[%s] and CYCs=[%s] ..." % (
            ";".join([str(wmo) for wmo in WMOs]),
            ";".join([str(cyc) for cyc in CYCs])))
        self.load()
        self.search_type = {'WMO': WMOs, 'CYC': CYCs}
        filt = []
        for wmo in WMOs:
            for cyc in CYCs:
                if cyc < 1000:
                    pattern = "%i_%0.3d.nc" % (wmo, cyc)
                else:
                    pattern = "%i_%0.4d.nc" % (wmo, cyc)
                filt.append(self.index['file'].str.contains(pattern, regex=True, case=False))
        self.search_filter = np.logical_or.reduce(filt)
        self.run(nrows=nrows)
        return self

    def search_tim(self, BOX, nrows=None):
        is_indexbox(BOX)
        log.debug("Argo index searching for time in BOX=%s ..." % BOX)
        self.load()
        self.search_type = {'BOX': BOX}
        tim_min = int(pd.to_datetime(BOX[4]).strftime("%Y%m%d%H%M%S"))
        tim_max = int(pd.to_datetime(BOX[5]).strftime("%Y%m%d%H%M%S"))
        filt = []
        filt.append(self.index['date'].ge(tim_min))
        filt.append(self.index['date'].le(tim_max))
        self.search_filter = np.logical_and.reduce(filt)
        self.run(nrows=nrows)
        return self

    def search_lat_lon(self, BOX, nrows=None):
        is_indexbox(BOX)
        log.debug("Argo index searching for lat/lon in BOX=%s ..." % BOX)
        self.load()
        self.search_type = {'BOX': BOX}
        filt = []
        filt.append(self.index['longitude'].ge(BOX[0]))
        filt.append(self.index['longitude'].le(BOX[1]))
        filt.append(self.index['latitude'].ge(BOX[2]))
        filt.append(self.index['latitude'].le(BOX[3]))
        self.search_filter = np.logical_and.reduce(filt)
        self.run(nrows=nrows)
        return self

    def search_lat_lon_tim(self, BOX, nrows=None):
        is_indexbox(BOX)
        log.debug("Argo index searching for lat/lon/time in BOX=%s ..." % BOX)
        self.load()
        self.search_type = {'BOX': BOX}
        tim_min = int(pd.to_datetime(BOX[4]).strftime("%Y%m%d%H%M%S"))
        tim_max = int(pd.to_datetime(BOX[5]).strftime("%Y%m%d%H%M%S"))
        filt = []
        filt.append(self.index['date'].ge(tim_min))
        filt.append(self.index['date'].le(tim_max))
        filt.append(self.index['longitude'].ge(BOX[0]))
        filt.append(self.index['longitude'].le(BOX[1]))
        filt.append(self.index['latitude'].ge(BOX[2]))
        filt.append(self.index['latitude'].le(BOX[3]))
        self.search_filter = np.logical_and.reduce(filt)
        self.run(nrows=nrows)
        return self
