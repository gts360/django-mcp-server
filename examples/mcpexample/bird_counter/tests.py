from django.db.models import Q
from django.test import TestCase

from .models import Bird, Location, City
from mcp_server import query_tool


class JSONQueryTest(TestCase):

    def setUp(self):
        cities = City.objects.bulk_create([
            City(name='New York', country='USA'),
            City(name='Paris', country='FRA')
        ])
        locs = Location.objects.bulk_create([
            Location(name='Park', description='A large park', city=cities[0]),
            Location(name='Forest', description='A dense forest', city=cities[1]),
            Location(name='Beach', description='A sunny beach', city=cities[0]),
        ])
        Bird.objects.bulk_create([
            Bird(location=locs[0], species='Sparrow', count=5),
            Bird(location=locs[0], species='Robin', count=3),
            Bird(location=locs[1], species='Eagle', count=2),
            Bird(location=locs[1], species='Hawk', count=4),
            Bird(location=locs[2], species='Seagull', count=10),
            Bird(location=locs[2], species='Pelican', count=7),
            Bird(location=locs[0], species='Pigeon', count=8),
            Bird(location=locs[1], species='Falcon', count=6),
            Bird(location=locs[2], species='Dove', count=1),
        ])

    def test_gen_schema(self):
        self.assertEqual(query_tool.generate_json_schema(Bird),
                         {
                             'description': 'Inventory of observation of a certain species of birds',
                             '$jsonSchema': {
                                 'bsonType': 'object',
                                 'properties': {
                                    'id': {
                                        'description': 'Primary unique identifier for this model',
                                        'bsonType': 'int'
                                    },
                                    'location': {
                                        'bsonType': 'objectId',
                                        'description': 'Reference to Location: The location of the observation',
                                        'ref': 'Location'
                                    },
                                    'species': {'bsonType': 'string'},
                                    'count': {'bsonType': 'int'}
                                 },
                                'required': ['species', 'count']}
                            }
                         )

    def _assert_bird_jsonquery_match(self, expectedqs, pipeline, count=None, **kwargs):
        birds = query_tool.apply_json_mango_query(Bird.objects.all().order_by("id"), pipeline, **kwargs)
        birds = list(birds)
        self.assertListEqual(list(expectedqs.values().order_by("id")), birds)
        if count is not None:
            self.assertEqual(count, len(birds))

    def test_query(self):
        # Test the query method
        birds = query_tool.apply_json_mango_query(Bird.objects.all(), [])
        birds = list(birds)
        self.assertEqual(Bird.objects.all().count(), len(birds))

        self._assert_bird_jsonquery_match(Bird.objects.filter(species__regex=".*r.*"),
        [
                {
                    "$match": {
                        "species": { "$regex": ".*r.*" }
                    }
                }
            ])

        self._assert_bird_jsonquery_match(Bird.objects.filter(species__regex=".*r.*"),
                                       [
                                              {
                                                  "$match": {
                                                      "species": {"$regex": ".*r.*"}
                                                  }
                                              }
                                          ])

        self._assert_bird_jsonquery_match(Bird.objects.filter(species__regex=".*r.*", count__gte=5),
                                          [
                                              {
                                                  "$match": {
                                                      "species": {"$regex": ".*r.*"},
                                                      "count": {"$gte": 5}
                                                  }
                                              }
                                          ], 1)

        self._assert_bird_jsonquery_match(Bird.objects.filter(Q(species__regex=".*r.*", count__gte=5)|Q(count__lt=3)),
                                          [
                                              {
                                                  "$match": {
                                                      "$or": [
                                                          { "species": {"$regex": ".*r.*"} },
                                                          { "count": {"$lt": 3} }
                                                    ]
                                                  }
                                              }
                                          ], 3)

    def test_query_with_lookup(self):
        self._assert_bird_jsonquery_match(
            Bird.objects.filter(Q(location__name="Forest") | Q(count__lt=3)),
            [
                {
                    "$lookup": {
                        "from": "location",
                        "localField": "location",
                        "foreignField": "_id",
                        "as": "loc"
                    }
                },
                {
                    "$match": {
                        "$or": [
                            {"count": {"$lt": 3}},
                            {"loc.name": "Forest"}
                        ]
                    }
                }
            ], 4)

        self._assert_bird_jsonquery_match(
            Bird.objects.filter(Q(location__city__country="USA")),
            [
                {
                    "$lookup": {
                        "from": "location",
                        "localField": "location",
                        "foreignField": "_id",
                        "as": "loc"
                    }
                },{
                    "$lookup": {
                        "from": "city",
                        "localField": "loc.city",
                        "foreignField": "_id",
                        "as": "city"
                    }
                },
                {
                    "$match": {"city.country": "USA"}
                }
            ], 6)

    def test_projection(self):
        res = query_tool.apply_json_mango_query(
            Bird.objects.all(),[
                {
                    "$lookup": {
                        "from": "location",
                        "localField": "location",
                        "foreignField": "_id",
                        "as": "loc"
                    }
                },{
                    "$lookup": {
                        "from": "city",
                        "localField": "loc.city",
                        "foreignField": "_id",
                        "as": "city"
                    }
                },
                {
                    "$match": {
                        "species": "Eagle"
                    }
                },
                {
                    "$project": {
                        "_id": 0,
                        "species": 1,
                        "country": "$city.country",
                    }
                }
            ]
        )

        res = list(res)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]['species'], "Eagle")
        self.assertEqual(res[0]['country'], "FRA")


    def test_fulltext_search_stage(self):
        self._assert_bird_jsonquery_match(
            Bird.objects.filter(species__in=['Falcon','Eagle']), [
                {
                    "$search": {
                        "text": {
                            "query": "For l"
                        }
                    }
                }
            ],
            text_search_fields=['species','location__name']
        )

    def test_fulltext_search_match_text_op(self):
        res = self._assert_bird_jsonquery_match(
            Bird.objects.filter(species__in=['Falcon','Eagle']), [
                {
                    "$match": {
                        "$text": {
                            "$search": "l fore"
                        }
                    }
                }
            ],
            text_search_fields=['species','location__name']
        )

    def test_group_aggregations(self):
        pipeline = [
            {
                "$lookup": {
                    "from": "location",
                    "localField": "location",
                    "foreignField": "_id",
                    "as": "loc"
                }
            },
            {
                "$lookup": {
                    "from": "city",
                    "localField": "loc.city",
                    "foreignField": "_id",
                    "as": "city"
                }
            },{
                "$group": {
                    "_id": "$city.country",
                    "total": {"$sum": "$count"},
                    "average": {"$avg": "$count"},
                    "max_count": {"$max": "$count"},
                    "min_count": {"$min": "$count"},
                    "count": {"$count": 1}
                }
            }
        ]
        result = query_tool.apply_json_mango_query(Bird.objects.all(), pipeline)
        result = list(result)
        self.assertEqual(len(result), 2)  # Expect 2 distinct countries
        for row in result:
            self.assertTrue("city" in row and "country" in row["city"])
            self.assertEqual(set(row.keys()), {"total", "average", "count", "max_count", "min_count", "city"})
            if row["city"]["country"] == "USA":
                self.assertEqual(34, row["total"])
                self.assertEqual(10, row["max_count"])
                self.assertEqual(1, row["min_count"])
                self.assertEqual(6, row["count"] )
                self.assertEqual(5.666666666666667, row["average"])


    def test_group_aggregations(self):
        pipeline = [
            {
                "$lookup": {
                    "from": "location",
                    "localField": "location",
                    "foreignField": "_id",
                    "as": "loc"
                }
            },
            {
                "$lookup": {
                    "from": "city",
                    "localField": "loc.city",
                    "foreignField": "_id",
                    "as": "city"
                }
            },{
                "$group": {
                    "_id": None,
                    "total": {"$sum": "$count"},
                    "average": {"$avg": "$count"},
                    "max_count": {"$max": "$count"},
                    "min_count": {"$min": "$count"},
                    "count": {"$count": 1}
                }
            }
        ]
        result = query_tool.apply_json_mango_query(Bird.objects.all(), pipeline)
        result = list(result)
        self.assertEqual(len(result), 1)  # Expect 2 distinct countries
        row = result[0]
        self.assertEqual(set(row.keys()), {"total", "average", "count", "max_count", "min_count"})
        self.assertEqual(46, row["total"])
        self.assertEqual(10, row["max_count"])
        self.assertEqual(1, row["min_count"])
        self.assertEqual(9, row["count"] )
        self.assertEqual(5.111111111111111, row["average"])
        
        
    def test_group_multi_aggregations(self):
        # Add a location in france ot have more interesting stats
        loc=Location.objects.create(
            name="Place Broglie",
            city= City.objects.create(name='Strasbourg', country='FRA')
        )

        Bird.objects.bulk_create([
            Bird(location=loc, species='Sparrow', count=3),
            Bird(location=loc, species='Sparrow', count=5),
        ])
        pipeline = [
            {
                "$lookup": {
                    "from": "location",
                    "localField": "location",
                    "foreignField": "_id",
                    "as": "loc"
                }
            },
            {
                "$lookup": {
                    "from": "city",
                    "localField": "loc.city",
                    "foreignField": "_id",
                    "as": "city"
                }
            },{
                "$group": {
                    "_id": {
                        "country": "$city.country",
                        "species": "$species"
                    },
                    "total": {"$sum": "$count"},
                    "average": {"$avg": "$count"},
                    "max_count": {"$max": "$count"},
                    "min_count": {"$min": "$count"},
                    "count": {"$count": 1}
                }
            }
        ]
        result = query_tool.apply_json_mango_query(Bird.objects.all(), pipeline)
        result = list(result)
        print(result)
        self.assertEqual(len(result), len(set(Bird.objects.values_list("location__city__country","species"))))  # Expect 2 distinct countries/species combinations

        row = next(res for res in result if res["_id"]["country"] == "FRA" and res["_id"]["species"]=="Sparrow")

        self.assertEqual(set(row.keys()), {"_id", "total", "average", "count", "max_count", "min_count"})
        self.assertEqual(8, row["total"])
        self.assertEqual(5, row["max_count"])
        self.assertEqual(3, row["min_count"])
        self.assertEqual(2, row["count"] )
        self.assertEqual(4, row["average"])


class UndeclaredFieldsNotReturnedTest(TestCase):
    """Undeclared fields must never appear in query output.

    `ModelQueryToolset.fields` is documented as controlling which fields are
    published. Before this change it only affected the LLM-facing schema; the
    default `queryset.values()` call at the end of the pipeline still returned
    every concrete column on the model. That meant any column on any row a
    caller could see — internal flags, audit metadata, raw geometry columns,
    anything the toolset author deliberately left out of `fields` — was
    reachable by simply omitting `$project`. It also made the endpoint crash
    whenever a non-declared column was non-serializable (e.g. GeoDjango
    PointField), since the column still reached the JSON renderer.

    These tests pin the guarantee that `fields` is a real boundary: rows
    returned from the pipeline never contain keys outside the declared set.
    """

    def setUp(self):
        city = City.objects.create(name='New York', country='USA')
        loc = Location.objects.create(name='Park', city=city)
        Bird.objects.create(location=loc, species='Sparrow', count=5)
        Bird.objects.create(location=loc, species='Robin', count=3)

    def test_undeclared_fields_are_not_returned(self):
        """The core guarantee: `count` and `location_id` exist on Bird but
        aren't in the allowlist, so they must not leak into the output."""
        rows = list(query_tool.apply_json_mango_query(
            Bird.objects.all().order_by("id"), [],
            allowed_fields={"id", "species"},
        ))
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(set(row.keys()), {"id", "species"})
            self.assertNotIn("count", row)
            self.assertNotIn("location_id", row)

    def test_explicit_project_to_declared_field_works(self):
        rows = list(query_tool.apply_json_mango_query(
            Bird.objects.all().order_by("id"),
            [{"$project": {"species": 1}}],
            allowed_fields={"id", "species"},
        ))
        self.assertEqual([r["species"] for r in rows], ["Sparrow", "Robin"])
        for row in rows:
            self.assertNotIn("id", row)
            self.assertNotIn("count", row)

    def test_project_to_undeclared_field_is_silently_stripped(self):
        """`$project: {count: 1}` selects an undeclared column directly. The
        mapping is dropped before interpretation so `count` never reaches
        the output — even though the row-level access would otherwise allow
        reading it."""
        rows = list(query_tool.apply_json_mango_query(
            Bird.objects.all().order_by("id"),
            [{"$project": {"species": 1, "count": 1}}],
            allowed_fields={"id", "species"},
        ))
        for row in rows:
            self.assertNotIn("count", row)
            self.assertIn("species", row)

    def test_project_renamed_from_undeclared_field_is_silently_stripped(self):
        """Rename bypass: `{renamed: "$count"}` still references `count` as
        the source. Stripping by source prevents the output key `renamed`
        from appearing at all."""
        rows = list(query_tool.apply_json_mango_query(
            Bird.objects.all().order_by("id"),
            [{"$project": {"species": 1, "renamed": "$count"}}],
            allowed_fields={"id", "species"},
        ))
        for row in rows:
            self.assertNotIn("renamed", row)
            self.assertNotIn("count", row)
            self.assertIn("species", row)

    def test_no_allowed_fields_preserves_legacy_behaviour(self):
        """When a toolset doesn't declare `fields`, the caller opted out of
        the allowlist — output contains every concrete column, same as
        previous versions. This is the escape hatch for existing callers."""
        rows = list(query_tool.apply_json_mango_query(Bird.objects.all().order_by("id"), []))
        for row in rows:
            self.assertIn("count", row)
            self.assertIn("location_id", row)