''':mod:`streamcorpus_pipeline` tagger stage for OpenSextant

.. This software is released under an MIT/X11 open source license.
   Copyright 2014 Diffeo, Inc.

This provides a connector to use the OpenSextantToolbox
https://github.com/OpenSextant/OpenSextantToolbox/ as a tagger in
:mod:`streamcorpus_pipeline`.  Typical configuration looks like:

.. code-block:: yaml

    streamcorpus_pipeline:
      reader: from_local_chunks
      incremental_transforms: [language, guess_media_type, clean_html,
                               title, hyperlink_labels, clean_visible,
                               opensextant]
      batch_transforms: [multi_token_match_align_labels]
      writers: [to_local_chunks]
      opensextant:
        path_in_third: opensextant/opensextant-current
      multi_token_match_align_labels:
        annotator_id: author
        tagger_id: opensextant

The ``opensextant`` stage is an incremental transform.  Failures on
individual stream items will result in those stream items remaining in
the stream, but without any tagging.  

Note that this stage does *not* run its own aligner, unlike older
tagger stages.  If desired, you must explicitly include an aligner in
``batch_transforms`` to convert document-level
:class:`streamcorpus.Rating` objects to token-level
:class:`streamcorpus.Token` objects.

For all stages that expect a tagger ID, this uses a tagger ID of
``opensextant``.  The stage has no configuration beyond `rest_url`

.. autoclass:: OpenSextantTagger
   :show-inheritance:

'''
from __future__ import absolute_import
import itertools
import json
import logging
import os.path
import sys
import time
import traceback

import requests
from sortedcollection import SortedCollection

from streamcorpus import Chunk, Tagging, Sentence, Token, make_stream_time, \
    OffsetType, EntityType, MentionType
from streamcorpus_pipeline.stages import IncrementalTransform

logger = logging.getLogger('streamcorpus_pipeline' + '.' + __name__)


class OpenSextantTagger(IncrementalTransform):
    ''':mod:`streamcorpus_pipeline` tagger stage for OpenSextant.

    This is an incremental transform, and needs to be included in the
    ``incremental_transforms`` list to run within
    :mod:`streamcorpus_pipeline`.
    
    .. automethod:: __init__
    .. automethod:: process_path
    .. automethod:: shutdown

    '''

    config_name = 'opensextant'
    tagger_id = 'opensextant'

    default_config = {
        'scheme': 'http',
        'network_address': 'localhost:8182',
        'service_path': '/opensextant/extract/general/json',
        'verify_ssl': False,
        'username': None,
        'password': None,
        'cert': None
    }

    def __init__(self, config, *args, **kwargs):
        '''Create a new tagger.

        `config` should provides ``scheme``, ``network_address``, and
        ``service_path``, which are assembled into a URL for POSTing
        :attr:`~streamcorpus.StreamItem.body.clean_visible` to obtain
        JSON.  The defaults provide this URL:
        `http://localhost:8182/opensextant/extract/general/json`.

        Optionally, `config` can also contain `verify_ssl` with a path
        to a cert.ca-bundle file to verify the remote server's SSL
        cert.  This is useful if the OpenSextant tagger is proxied
        behind an SSL gateway.  By default, `verify_ssl` is False.

        Optionally, `config` can also contain `username` and
        `password` for BasicAuth to access the OpenSextent end point.

        Per the python `requests` documentation, you can also specify
        a local cert to use as client side certificate, as a single
        file (containing the private key and the certificate) or as a
        tuple of both file's path `cert=('cert.crt', 'cert.key')`

        :param dict config: local configuration dictionary

        '''
        super(OpenSextantTagger, self).__init__(config, *args, **kwargs)
        kwargs = {}
        self.rest_url = config['scheme'] + '://' + config['network_address'] \
                        + config['service_path']
        self.verify_ssl = config['verify_ssl']

        ## Session carries connection pools that automatically provide
        ## HTTP keep-alive, so we can send many documents over one
        ## connection.
        self.session = requests.Session()
        username = config.get('username')
        password = config.get('password')
        if username and password:
            self.session.auth = HTTPBasicAuth(username, password)

        cert = config.get('cert')
        if cert and isinstance(cert, (list, tuple)):
            self.session.cert = tuple(cert)
        elif cert:
            self.session.cert = cert


    def process_item(self, si, context=None):
        '''Run OpenSextant over a single stream item.

        This ignores the `context`, and always returns the input
        stream item `si`.  Its sole action is to add a ``opensextant``
        value to the tagger-keyed fields in `si.body`, provided that
        `si` in fact has a
        :attr:`~streamcorpus.ContentItem.clean_visible` part.

        :param si: stream item to process
        :paramtype si: :class:`streamcorpus.StreamItem`
        :param dict context: additional shared context data
        :return: `si`

        '''
        if si.body and si.body.clean_visible:
            # clean_visible will be UTF-8 encoded
            logger.debug('POST %d bytes of clean_visible to %s',
                         len(si.body.clean_visible), self.rest_url)
            response = self.session.post(
                self.rest_url,
                data=si.body.clean_visible,
                verify=self.verify_ssl,
                headers={},
                timeout=10,
            )
            result = json.loads(response.content)

            # TODO: write make_tagging and make_sentences to parse the JSON respons
            si.body.taggings[self.tagger_id] = self.make_tagging(result)
            si.body.sentences[self.tagger_id] = annotate_sentences(si, result)

            #si.body.relations[self.tagger_id] = make_relations(result)
            #si.body.attributes[self.tagger_id] = make_attributes(result)

        return si

    def shutdown(self):
        '''Try to stop processing.

        Does nothing, since all of the work is done in-process.
        
        '''
        pass

    def make_tagging(self, result):
        return Tagging(
            tagger_id=self.tagger_id,
            tagger_version='2.1',
            generation_time=make_stream_time(time.time()),
        )


def annotate_sentences(si, result):
    logger.info(json.dumps(result, indent=4, sort_keys=4))
    #sys.exit()
    
    sentences = si.body.sentences['nltk_tokenizer']
    toks = SortedCollection(
        itertools.chain(*[sent.tokens for sent in sentences]),
        key=lambda tok: tok.offsets[OffsetType.BYTES].first
        )

    mention_id = 0
    for anno in result.get('annoList', []):
        if not anno.get('features', {}).get('isEntity'): 
            logger.debug('skipping isEntity=False: %s', 
                         json.dumps(anno, indent=4, sort_keys=True))
            continue
        start = anno['start']
        end = anno['end']
        if not si.body.clean_visible.decode('utf8')[start:end] == anno['matchText']:
            logger.critical('alignment failure: %r (take 3 chars off ends) != %r',
                            si.body.clean_visible.decode('utf8')[start-3:end+3], 
                            anno['matchText'])
        
        for tok in toks.find_range(start, end):
            fhierarchy = anno['features']['hierarchy']
            fh_parts = fhierarchy.split('.')
            if entity_types.get(fhierarchy):
                e_type, m_type = entity_types[fhierarchy]
            elif entity_types.get(fh_parts[0]):
                e_type, m_type = entity_types[fh_parts[0]]
            else:
                e_type, m_type = None, None

            if e_type is not None:
                tok.entity_type = e_type
                tok.mention_type = m_type
                tok.mention_id = mention_id
                ## too bad no coref chains, so nominals are not connected
                ## to names:
                tok.equiv_id = mention_id  

        mention_id += 1


entity_types = {
    ## most events are unnamed, so default to NOM
    'Action': (EntityType.EVENT, MentionType.NOM),

    'Attribute.attribute.measurableCharacteristic': None,
    'Attribute.weight': None,

    ## descriptive attributes --> nominatives
    'Geo.area': (EntityType.LOC, MentionType.NOM),
    'Geo.distance': (EntityType.LOC, MentionType.NOM),
    'Geo.weather': (EntityType.LOC, MentionType.NOM),

    ## most GEO area named locations
    'Geo.featureType.AdminRegion': (EntityType.LOC, MentionType.NAME),
    'Geo.featureType.Area': (EntityType.LOC, MentionType.NAME),
    'Geo.featureType.Hydro': (EntityType.LOC, MentionType.NAME),
    'Geo.featureType.Hypso': (EntityType.LOC, MentionType.NAME),
    'Geo.featureType.Misc': (EntityType.LOC, MentionType.NAME),
    'Geo.featureType.PopulatedPlace': (EntityType.LOC, MentionType.NAME),
    'Geo.featureType.Street': (EntityType.LOC, MentionType.NAME),
    'Geo.featureType.Undersea': (EntityType.LOC, MentionType.NAME),
    'Geo.featureType.Vegetation': (EntityType.LOC, MentionType.NAME),

    'Geo.place.geocoordinate': (EntityType.LOC, MentionType.NAME),
    'Geo.place.namedPlace': (EntityType.LOC, MentionType.NAME),

    ## these are usually named facilities
    'Geo.featureType.SpotFeature': (EntityType.FAC, MentionType.NAME),
    'Geo.facilityComponents': (EntityType.FAC, MentionType.NAME),

    'Idea': None,
    'Information': None,
    'Object': None,

    'Organization': (EntityType.ORG, MentionType.NAME),
    'Person': (EntityType.PER, MentionType.NAME),

    ## would be nice to map these into relations
    'Person.attitude.emotion': None,
    'Person.attitude.emotion.negativeEmotion': None,
    'Person.attitude.emotion.positiveEmotion': None,
    'Person.bodyPart': None,
    'Person.ethnicity': None,
    'Person.health': None,
    'Person.health.disease': None,
    'Person.health.injury': None,
    'Person.jobOrRole': None,
    'Person.language': None,

    'Person.name.personName': (EntityType.PER, MentionType.NAME),

    'Person.name.title.corporateTitle': (EntityType.PER, MentionType.NOM),
    'Person.name.title.governmentTitle': (EntityType.PER, MentionType.NOM),
    'Person.name.title.hereditaryTitle': (EntityType.PER, MentionType.NOM),
    'Person.name.title.militaryTitle': (EntityType.PER, MentionType.NOM),
    'Person.name.title.personalTitle': (EntityType.PER, MentionType.NOM),
    'Person.name.title.religiousTitle': (EntityType.PER, MentionType.NOM),

    'Person.relative': None,

    'Substance': None,
    'Time': None,
}


## this list of hierarchical entity types is copied from
## https://github.com/OpenSextant/OpenSextantToolbox/blob/master/LanguageResources/docs/
entity_hierarchy = {
    'Action.event': None,
    'Action.event.crime': None,
    'Action.event.disaster': None,
    'Action.event.legalEvent': None,
    'Action.event.meetingEvent': None,
    'Action.event.militaryEvent': None,
    'Action.event.movement': None,
    'Action.event.politicalEvent': None,
    'Action.event.socialEvent': None,
    'Action.event.violentEvent': None,
    'Attribute.attribute.measurableCharacteristic': None,
    'Attribute.weight': None,
    'Geo.area': None,
    'Geo.distance': None,
    'Geo.facilityComponents': None,
    'Geo.featureType.AdminRegion': None,
    'Geo.featureType.Area': None,
    'Geo.featureType.Hydro': None,
    'Geo.featureType.Hypso': None,
    'Geo.featureType.Misc': None,
    'Geo.featureType.PopulatedPlace': None,
    'Geo.featureType.SpotFeature': None,
    'Geo.featureType.Street': None,
    'Geo.featureType.Undersea': None,
    'Geo.featureType.Vegetation': None,
    'Geo.place.geocoordinate': None,
    'Geo.place.namedPlace': None,
    'Geo.weather': None,
    'Idea.fieldOfStudy': None,
    'Idea.idea': None,
    'Idea.ideology.politicalIdealogy': None,
    'Idea.ideology.socialIdealogy': None,
    'Information': None,
    'Information.identifier': None,
    'Information.identifier.documentTitle': None,
    'Information.identifier.MACAddress': None,
    'Information.identifier.telephoneNumber': None,
    'Information.informationArtifact': None,
    'Information.software': None,
    'Information.web.emailAddress': None,
    'Information.web.IPAddress': None,
    'Information.web.url': None,
    'Information.web.webSite': None,
    'Object': None,
    'Object.animal': None,
    'Object.clothing': None,
    'Object.container': None,
    'Object.debris': None,
    'Object.electronics': None,
    'Object.equipment': None,
    'Object.equipment.constructionEquipment': None,
    'Object.equipment.tool': None,
    'Object.finance.financialInstrument': None,
    'Object.finance.money': None,
    'Object.finance.money': None,
    'Object.food': None,
    'Object.vehicle': None,
    'Object.vehicle.aircraft': None,
    'Object.vehicle.aircraft.combatAircraft': None,
    'Object.vehicle.aircraft.combatSupportAircraft': None,
    'Object.vehicle.aircraft.helicopter': None,
    'Object.vehicle.emergencyVehicle': None,
    'Object.vehicle.militaryVehicle': None,
    'Object.vehicle.militaryVehicle.armoredVehicle': None,
    'Object.vehicle.ship': None,
    'Object.vehicle.spacecraft': None,
    'Object.vehicle.submarine': None,
    'Object.weapon': None,
    'Object.weapon.explosive': None,
    'Object.weapon.firearm': None,
    'Object.weapon.weaponOfMassDestruction': None,
    'Organization': None,
    'Organization.corporateOrganization': None,
    'Organization.criminalOrganization': None,
    'Organization.governmentOrganization': None,
    'Organization.governmentOrganization.politicalParty': None,
    'Organization.governmentOrganization.USGovernmentOrganization': None,
    'Organization.informalOrganization': None,
    'Organization.internationalOrganization': None,
    'Organization.media.newspaper': None,
    'Organization.militantGroup': None,
    'Organization.militaryOrganization': None,
    'Organization.religion': None,
    'Organization.terroristGroup': None,
    'Person': None,
    'Person.attitude.emotion': None,
    'Person.attitude.emotion.negativeEmotion': None,
    'Person.attitude.emotion.positiveEmotion': None,
    'Person.bodyPart': None,
    'Person.ethnicity': None,
    'Person.health': None,
    'Person.health.disease': None,
    'Person.health.injury': None,
    'Person.jobOrRole': None,
    'Person.language': None,
    'Person.name.personName': None,
    'Person.name.personName': None,
    'Person.name.title.corporateTitle': None,
    'Person.name.title.governmentTitle': None,
    'Person.name.title.hereditaryTitle': None,
    'Person.name.title.militaryTitle': None,
    'Person.name.title.personalTitle': None,
    'Person.name.title.religiousTitle': None,
    'Person.relative': None,
    'Substance': None,
    'Substance.chemical': None,
    'Substance.drug': None,
    'Substance.material': None,
    'Time.date': None,
    'Time.date': None,
    'Time.dayOfTheWeek': None,
    'Time.holiday': None,
    'Time.lengthOfTime': None,
    'Time.month': None,
    'Time.season': None,
    'Time.time': None,
    'Time.timePhrase': None,
}

