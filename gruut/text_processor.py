#!/usr/bin/env python3
"""Tokenizes, verbalizes, and phonemizes text and SSML"""
import functools
import logging
import re
import typing
import xml.etree.ElementTree as etree
from decimal import Decimal
from pathlib import Path
from xml.sax import saxutils

import babel
import babel.numbers
import dateparser
import networkx as nx
from gruut_ipa import IPA
from num2words import num2words

from gruut.const import (
    DATA_PROP,
    PHONEMES_TYPE,
    REGEX_PATTERN,
    BreakNode,
    BreakType,
    BreakWordNode,
    EndElement,
    GraphType,
    IgnoreNode,
    InterpretAs,
    InterpretAsFormat,
    Node,
    ParagraphNode,
    PunctuationWordNode,
    Sentence,
    SentenceNode,
    SpeakNode,
    TextProcessorSettings,
    Word,
    WordNode,
    WordRole,
)
from gruut.lang import get_settings
from gruut.utils import (
    attrib_no_namespace,
    grouper,
    leaves,
    pipeline_split,
    pipeline_transform,
    resolve_lang,
    tag_no_namespace,
    text_and_elements,
)

# -----------------------------------------------------------------------------

_LOGGER = logging.getLogger("gruut.text_processor")


# -----------------------------------------------------------------------------


class TextProcessor:
    """Tokenizes, verbalizes, and phonemizes text and SSML"""

    def __init__(
        self,
        default_lang: str = "en_US",
        model_prefix: str = "",
        lang_dirs: typing.Optional[typing.Dict[str, typing.Union[str, Path]]] = None,
        search_dirs: typing.Optional[typing.Iterable[typing.Union[str, Path]]] = None,
        settings: typing.Optional[
            typing.MutableMapping[str, TextProcessorSettings]
        ] = None,
        **kwargs,
    ):
        self.default_lang = default_lang
        self.default_settings_kwargs = kwargs

        self.model_prefix = model_prefix
        self.search_dirs = search_dirs

        if lang_dirs is None:
            lang_dirs = {}

        self.lang_dirs = lang_dirs

        if settings is None:
            settings = {}

        self.settings = settings

    def sentences(
        self,
        graph: GraphType,
        root: Node,
        major_breaks: bool = True,
        minor_breaks: bool = True,
        punctuations: bool = True,
        explicit_lang: bool = True,
        phonemes: bool = True,
        break_phonemes: bool = True,
        pos: bool = True,
    ) -> typing.Iterable[Sentence]:
        """Processes text and returns each sentence"""

        def get_lang(lang: str) -> str:
            if explicit_lang or (lang != self.default_lang):
                return lang

            # Implicit default language
            return ""

        def make_sentence(
            node: Node, words: typing.Sequence[Word], sent_idx: int
        ) -> Sentence:
            settings = self.get_settings(node.lang)
            text_with_ws = "".join(w.text_with_ws for w in words)
            text = settings.normalize_whitespace(text_with_ws)
            sent_voice = ""

            # Get voice used across all words
            for word in words:
                if word.voice:
                    if sent_voice and (sent_voice != word.voice):
                        # Multiple voices
                        sent_voice = ""
                        break

                    sent_voice = word.voice

            if sent_voice:
                # Set voice on all words
                for word in words:
                    word.voice = sent_voice

            return Sentence(
                idx=sent_idx,
                text=text,
                text_with_ws=text_with_ws,
                lang=get_lang(node.lang),
                voice=sent_voice,
                words=words,
            )

        sent_idx: int = 0
        word_idx: int = 0
        words: typing.List[Word] = []
        last_sentence_node: typing.Optional[Node] = None

        for dfs_node in nx.dfs_preorder_nodes(graph, root.node):
            node = graph.nodes[dfs_node][DATA_PROP]
            if isinstance(node, SentenceNode):
                if words and (last_sentence_node is not None):
                    yield make_sentence(last_sentence_node, words, sent_idx)
                    sent_idx += 1
                    word_idx = 0
                    words = []

                last_sentence_node = node
            elif graph.out_degree(dfs_node) == 0:
                if isinstance(node, WordNode):
                    word = typing.cast(WordNode, node)
                    words.append(
                        Word(
                            idx=word_idx,
                            sent_idx=sent_idx,
                            text=word.text,
                            text_with_ws=word.text_with_ws,
                            phonemes=word.phonemes if phonemes else None,
                            pos=word.pos if pos else None,
                            lang=get_lang(node.lang),
                            voice=node.voice,
                        )
                    )

                    word_idx += 1
                elif isinstance(node, BreakWordNode):
                    break_word = typing.cast(BreakWordNode, node)
                    if (
                        minor_breaks and (break_word.break_type == BreakType.MINOR)
                    ) or (major_breaks and (break_word.break_type == BreakType.MAJOR)):
                        words.append(
                            Word(
                                idx=word_idx,
                                sent_idx=sent_idx,
                                text=break_word.text,
                                text_with_ws=break_word.text_with_ws,
                                phonemes=self._phonemes_for_break(
                                    break_word.break_type, lang=break_word.lang
                                )
                                if phonemes and break_phonemes
                                else None,
                                is_break=True,
                                lang=get_lang(node.lang),
                            )
                        )

                        word_idx += 1
                elif punctuations and isinstance(node, PunctuationWordNode):
                    punct_word = typing.cast(PunctuationWordNode, node)
                    words.append(
                        Word(
                            idx=word_idx,
                            sent_idx=sent_idx,
                            text=punct_word.text,
                            text_with_ws=punct_word.text_with_ws,
                            is_punctuation=True,
                            lang=get_lang(punct_word.lang),
                        )
                    )

                    word_idx += 1

        if words and (last_sentence_node is not None):
            yield make_sentence(last_sentence_node, words, sent_idx)

    def words(self, graph: GraphType, root: Node, **kwargs) -> typing.Iterable[Word]:
        """Processes text and returns each word"""
        for sent in self.sentences(graph, root, **kwargs):
            for word in sent:
                yield word

    def get_settings(self, lang: typing.Optional[str] = None) -> TextProcessorSettings:
        """Gets or creates settings for a language"""
        lang = lang or self.default_lang
        lang_settings = self.settings.get(lang)

        if lang_settings is not None:
            return lang_settings

        # Try again with resolved language
        resolved_lang = resolve_lang(lang)
        lang_settings = self.settings.get(resolved_lang)
        if lang_settings is not None:
            # Patch for the future
            self.settings[lang] = self.settings[resolved_lang]
            return lang_settings

        _LOGGER.debug(
            "No settings for language %s (%s). Creating default settings.",
            lang,
            resolved_lang,
        )

        # Create default settings for language
        lang_dir = self.lang_dirs.get(lang)
        lang_settings = get_settings(
            lang,
            lang_dir=lang_dir,
            model_prefix=self.model_prefix,
            search_dirs=self.search_dirs,
            **self.default_settings_kwargs,
        )
        self.settings[lang] = lang_settings
        self.settings[resolved_lang] = lang_settings

        return lang_settings

    # -------------------------------------------------------------------------
    # Processing
    # -------------------------------------------------------------------------

    def __call__(
        self,
        text: str,
        lang: typing.Optional[str] = None,
        ssml: bool = False,
        pos: bool = True,
        phonemize: bool = True,
        post_process: bool = True,
        add_speak_tag: bool = True,
    ) -> typing.Tuple[GraphType, Node]:
        """Processes text and SSML"""
        if not ssml:
            # Not XML
            text = saxutils.escape(text)

        if add_speak_tag and (not text.lstrip().startswith("<")):
            # Wrap in <speak> tag
            text = f"<speak>{text}</speak>"

        root_element = etree.fromstring(text)
        graph = typing.cast(GraphType, nx.DiGraph())

        # Parse XML
        last_paragraph: typing.Optional[ParagraphNode] = None
        last_sentence: typing.Optional[SentenceNode] = None
        last_speak: typing.Optional[SpeakNode] = None
        root: typing.Optional[SpeakNode] = None

        # [voice]
        voice_stack: typing.List[str] = []

        # [(interpret_as, format)]
        say_as_stack: typing.List[typing.Tuple[str, str]] = []

        # [(tag, lang)]
        lang_stack: typing.List[typing.Tuple[str, str]] = []
        current_lang: str = lang or self.default_lang

        # True if currently inside <w> or <token>
        in_word: bool = False

        # True if current word is the last one
        is_last_word: bool = False

        # Current word's role
        word_role: typing.Optional[str] = None

        # Alias from <sub>
        last_alias: typing.Optional[str] = None

        # Used to skip <metadata>
        skip_elements: bool = False

        # Create __init__ args for new Node
        def scope_kwargs(target_class):
            scope = {}
            if voice_stack:
                scope["voice"] = voice_stack[-1]

            scope["lang"] = current_lang

            if target_class is WordNode:
                if say_as_stack:
                    scope["interpret_as"], scope["format"] = say_as_stack[-1]

                if word_role is not None:
                    scope["role"] = word_role

            return scope

        # Process sub-elements and text chunks
        for elem_or_text in text_and_elements(root_element):
            if isinstance(elem_or_text, str):
                if skip_elements:
                    # Inside <metadata>
                    continue

                # Text chunk
                text = typing.cast(str, elem_or_text)

                if last_alias is not None:
                    # Iniside a <sub>
                    text = last_alias

                if last_speak is None:
                    # Implicit <speak>
                    last_speak = SpeakNode(node=len(graph), implicit=True)
                    graph.add_node(last_speak.node, data=last_speak)
                    if root is None:
                        root = last_speak

                assert last_speak is not None

                if last_paragraph is None:
                    # Implicit <p>
                    p_node = ParagraphNode(
                        node=len(graph), implicit=True, **scope_kwargs(ParagraphNode)
                    )
                    graph.add_node(p_node.node, data=p_node)

                    graph.add_edge(last_speak.node, p_node.node)
                    last_paragraph = p_node

                assert last_paragraph is not None

                if last_sentence is None:
                    # Implicit <s>
                    s_node = SentenceNode(
                        node=len(graph), implicit=True, **scope_kwargs(SentenceNode)
                    )
                    graph.add_node(s_node.node, data=s_node)

                    graph.add_edge(last_paragraph.node, s_node.node)
                    last_sentence = s_node

                assert last_sentence is not None

                if in_word:
                    # No splitting
                    word_text = text
                    settings = self.get_settings(current_lang)
                    if (
                        settings.keep_whitespace
                        and (not is_last_word)
                        and (not word_text.endswith(settings.join_str))
                    ):
                        word_text += settings.join_str

                    word_node = WordNode(
                        node=len(graph),
                        text=word_text.strip(),
                        text_with_ws=word_text,
                        **scope_kwargs(WordNode),
                    )
                    graph.add_node(word_node.node, data=word_node)
                    graph.add_edge(last_sentence.node, word_node.node)
                else:
                    # Split by whitespace
                    self._pipeline_tokenize(
                        graph, last_sentence, text, scope_kwargs=scope_kwargs(WordNode),
                    )

            elif isinstance(elem_or_text, EndElement):
                # End of an element (e.g., </s>)
                end_elem = typing.cast(EndElement, elem_or_text)
                end_tag = tag_no_namespace(end_elem.element.tag)

                if end_tag == "voice":
                    if voice_stack:
                        voice_stack.pop()
                elif end_tag == "say-as":
                    if say_as_stack:
                        say_as_stack.pop()
                else:
                    if lang_stack and (lang_stack[-1][0] == end_tag):
                        lang_stack.pop()

                    if lang_stack:
                        current_lang = lang_stack[-1][1]  # tag, lang
                    else:
                        current_lang = self.default_lang

                    if end_tag in {"w", "token"}:
                        # End of word
                        in_word = False
                        is_last_word = False
                        word_role = None
                    elif end_tag == "s":
                        # End of sentence
                        last_sentence = None
                    elif end_tag == "p":
                        # End of paragraph
                        last_paragraph = None
                    elif end_tag == "speak":
                        # End of speak
                        last_speak = root
                    elif end_tag == "s":
                        # End of sub
                        last_alias = None
                    elif end_tag == "metadata":
                        # End of metadata
                        skip_elements = False
            else:
                if skip_elements:
                    # Inside <metadata>
                    continue

                # Start of an element (e.g., <p>)
                elem, elem_metadata = elem_or_text
                elem = typing.cast(etree.Element, elem)

                # Optional metadata for the element
                elem_metadata = typing.cast(
                    typing.Optional[typing.Dict[str, typing.Any]], elem_metadata
                )

                elem_tag = tag_no_namespace(elem.tag)

                if elem_tag == "speak":
                    # Explicit <speak>
                    maybe_lang = attrib_no_namespace(elem, "lang")
                    if maybe_lang:
                        lang_stack.append((elem_tag, maybe_lang))
                        current_lang = maybe_lang

                    speak_node = SpeakNode(
                        node=len(graph), element=elem, **scope_kwargs(SpeakNode)
                    )
                    if root is None:
                        root = speak_node

                    graph.add_node(speak_node.node, data=root)
                    last_speak = root
                elif elem_tag == "voice":
                    # Set voice scope
                    voice_name = attrib_no_namespace(elem, "name")
                    voice_stack.append(voice_name)
                elif elem_tag == "p":
                    # Explicit paragraph
                    if last_speak is None:
                        # Implicit <speak>
                        last_speak = SpeakNode(node=len(graph), implicit=True)
                        graph.add_node(last_speak.node, data=last_speak)
                        if root is None:
                            root = last_speak

                    assert last_speak is not None

                    maybe_lang = attrib_no_namespace(elem, "lang")
                    if maybe_lang:
                        lang_stack.append((elem_tag, maybe_lang))
                        current_lang = maybe_lang

                    p_node = ParagraphNode(
                        node=len(graph), element=elem, **scope_kwargs(ParagraphNode)
                    )
                    graph.add_node(p_node.node, data=p_node)
                    graph.add_edge(last_speak.node, p_node.node)
                    last_paragraph = p_node
                elif elem_tag == "s":
                    # Explicit sentence
                    if last_speak is None:
                        # Implicit <speak>
                        last_speak = SpeakNode(node=len(graph), implicit=True)
                        graph.add_node(last_speak.node, data=last_speak)
                        if root is None:
                            root = last_speak

                    assert last_speak is not None

                    if last_paragraph is None:
                        # Implicit paragraph
                        p_node = ParagraphNode(
                            node=len(graph), **scope_kwargs(ParagraphNode)
                        )
                        graph.add_node(p_node.node, data=p_node)

                        graph.add_edge(last_speak.node, p_node.node)
                        last_paragraph = p_node

                    maybe_lang = attrib_no_namespace(elem, "lang")
                    if maybe_lang:
                        lang_stack.append((elem_tag, maybe_lang))
                        current_lang = maybe_lang

                    s_node = SentenceNode(
                        node=len(graph), element=elem, **scope_kwargs(SentenceNode)
                    )
                    graph.add_node(s_node.node, data=s_node)
                    graph.add_edge(last_paragraph.node, s_node.node)
                    last_sentence = s_node
                elif elem_tag in {"w", "token"}:
                    # Explicit word
                    in_word = True
                    is_last_word = (
                        elem_metadata.get("is_last", False) if elem_metadata else False
                    )
                    maybe_lang = attrib_no_namespace(elem, "lang")
                    if maybe_lang:
                        lang_stack.append((elem_tag, maybe_lang))
                        current_lang = maybe_lang

                    word_role = attrib_no_namespace(elem, "role")
                elif elem_tag == "break":
                    # Break
                    last_target = last_sentence or last_paragraph or last_speak
                    assert last_target is not None
                    break_node = BreakNode(
                        node=len(graph),
                        element=elem,
                        time=attrib_no_namespace(elem, "time", ""),
                    )
                    graph.add_node(break_node.node, data=break_node)
                    graph.add_edge(last_target.node, break_node.node)
                elif elem_tag == "say-as":
                    say_as_stack.append(
                        (
                            attrib_no_namespace(elem, "interpret-as", ""),
                            attrib_no_namespace(elem, "format", ""),
                        )
                    )
                elif elem_tag == "sub":
                    # Sub
                    last_alias = attrib_no_namespace(elem, "alias", "")
                elif elem_tag == "metadata":
                    # Metadata
                    skip_elements = True

        assert root is not None

        # Do replacements before minor/major breaks
        pipeline_split(self._split_replacements, graph, root)

        # Split punctuations 1/2 (quotes, etc.) before breaks
        pipeline_split(self._split_punctuations, graph, root)

        # Split on minor breaks (commas, etc.)
        pipeline_split(self._split_minor_breaks, graph, root)

        # Expand abbrevations before major breaks
        pipeline_split(self._split_abbreviations, graph, root)

        # Break apart initialisms 1/2 (e.g., TTS or T.T.S.) before major breaks
        split_initialism = functools.partial(
            self._split_initialism, phonemize=phonemize
        )
        pipeline_split(split_initialism, graph, root)

        # Split on major breaks (periods, etc.)
        pipeline_split(self._split_major_breaks, graph, root)

        # Split punctuations 2/2 (quotes, etc.) after breaks
        pipeline_split(self._split_punctuations, graph, root)

        # Break apart initialisms 2/2 (e.g., TTS. or T.T.S..) after major breaks
        pipeline_split(split_initialism, graph, root)

        # Break apart sentences using BreakWordNodes
        self._break_sentences(graph, root)

        # spell-out (e.g., abc -> a b c) before number expansion
        pipeline_split(self._split_spell_out, graph, root)

        # Transform text into known classes
        pipeline_transform(self._transform_number, graph, root)
        pipeline_transform(self._transform_currency, graph, root)
        pipeline_transform(self._transform_date, graph, root)

        # Verbalize known classes
        pipeline_transform(self._verbalize_number, graph, root)
        pipeline_transform(self._verbalize_currency, graph, root)
        pipeline_transform(self._verbalize_date, graph, root)

        # Break apart words
        pipeline_split(
            functools.partial(self._break_words, phonemize=phonemize), graph, root
        )

        # Ignore non-words
        pipeline_split(self._split_ignore_non_words, graph, root)

        # Gather words from leaves of the tree, group by sentence
        def process_sentence(words: typing.List[WordNode]):
            if pos:
                pos_settings = self.get_settings(node.lang)
                if pos_settings.get_parts_of_speech is not None:
                    pos_tags = pos_settings.get_parts_of_speech(
                        [word.text for word in words]
                    )
                    for word, pos_tag in zip(words, pos_tags):
                        word.pos = pos_tag

                        if not word.role:
                            word.role = f"gruut:{pos_tag}"

            if phonemize:
                # Add phonemes to word
                for word in words:
                    if word.phonemes:
                        # Word already has phonemes
                        continue

                    phonemize_settings = self.get_settings(word.lang)
                    if phonemize_settings.lookup_phonemes is not None:
                        word.phonemes = phonemize_settings.lookup_phonemes(
                            word.text, word.role
                        )

                    if (not word.phonemes) and (
                        phonemize_settings.guess_phonemes is not None
                    ):
                        word.phonemes = phonemize_settings.guess_phonemes(
                            word.text, word.role
                        )

        # Process tree leaves
        sentence_words: typing.List[WordNode] = []

        for dfs_node in nx.dfs_preorder_nodes(graph, root.node):
            node = graph.nodes[dfs_node][DATA_PROP]
            if isinstance(node, SentenceNode):
                if sentence_words:
                    process_sentence(sentence_words)
                    sentence_words = []
            elif graph.out_degree(dfs_node) == 0:
                if isinstance(node, WordNode):
                    word_node = typing.cast(WordNode, node)
                    sentence_words.append(word_node)

        if sentence_words:
            # Final sentence
            process_sentence(sentence_words)
            sentence_words = []

        if post_process:
            # Post-process sentences
            for dfs_node in nx.dfs_preorder_nodes(graph, root.node):
                node = graph.nodes[dfs_node][DATA_PROP]
                if isinstance(node, SentenceNode):
                    sent_node = typing.cast(SentenceNode, node)
                    sent_settings = self.get_settings(sent_node.lang)
                    if sent_settings.post_process_sentence is not None:
                        sent_settings.post_process_sentence(
                            graph, sent_node, sent_settings
                        )

            # Post process entire graph
            self.post_process_graph(graph, root)

        return graph, root

    def post_process_graph(self, graph: GraphType, root: Node):
        """User-defined post-processing of entire graph"""
        pass

    # -------------------------------------------------------------------------
    # Pipeline (custom)
    # -------------------------------------------------------------------------

    def _break_sentences(self, graph: GraphType, root: Node):
        """Break sentences apart at BreakWordNode(break_type="major") nodes."""

        # This involves:
        # 1. Identifying where in the edge list of sentence the break occurs
        # 2. Creating a new sentence next to the existing one in the parent paragraph
        # 3. Moving everything after the break into the new sentence
        for leaf_node in list(leaves(graph, root)):
            if not isinstance(leaf_node, BreakWordNode):
                # Not a break
                continue

            break_word_node = typing.cast(BreakWordNode, leaf_node)
            if break_word_node.break_type != BreakType.MAJOR:
                # Not a major break
                continue

            # Get the path from the break up to the nearest sentence
            parent_node: int = next(iter(graph.predecessors(break_word_node.node)))
            parent: Node = graph.nodes[parent_node][DATA_PROP]
            s_path: typing.List[Node] = [parent]

            while not isinstance(parent, SentenceNode):
                parent_node = next(iter(graph.predecessors(parent_node)))
                parent = graph.nodes[parent_node][DATA_PROP]
                s_path.append(parent)

            # Should at least be [WordNode, SentenceNode]
            assert len(s_path) >= 2
            s_node = s_path[-1]
            assert isinstance(s_node, SentenceNode)

            if not s_node.implicit:
                # Don't break apart explicit sentences
                continue

            # Probably a WordNode
            below_s_node = s_path[-2]

            # Edges after the break will need to be moved to the new sentence
            s_edges = list(graph.out_edges(s_node.node))
            break_edge_idx = s_edges.index((s_node.node, below_s_node.node))

            edges_to_move = s_edges[break_edge_idx + 1 :]
            if not edges_to_move:
                # Final sentence, nothing to move
                continue

            # Locate parent paragraph so we can create a new sentence
            p_node = self._find_parent(graph, s_node, ParagraphNode)
            assert p_node is not None

            # Find the index of the edge between the paragraph and the current sentence
            p_s_edge = (p_node.node, s_node.node)
            p_edges = list(graph.out_edges(p_node.node))
            s_edge_idx = p_edges.index(p_s_edge)

            # Remove existing edges from the paragraph
            graph.remove_edges_from(p_edges)

            # Create a sentence and add an edge to it right after the current sentence
            new_s_node = SentenceNode(node=len(graph), implicit=True)
            graph.add_node(new_s_node.node, data=new_s_node)
            p_edges.insert(s_edge_idx + 1, (p_node.node, new_s_node.node))

            # Insert paragraph edges with new sentence
            graph.add_edges_from(p_edges)

            # Move edges from current sentence to new sentence
            graph.remove_edges_from(edges_to_move)
            graph.add_edges_from([(new_s_node.node, v) for (u, v) in edges_to_move])

    def _break_words(self, graph: GraphType, node: Node, phonemize: bool = True):
        """Break apart words according to work breaks pattern"""
        if not isinstance(node, WordNode):
            return

        word = typing.cast(WordNode, node)
        if word.interpret_as:
            # Don't interpret words that are spoken for
            return

        if not word.implicit:
            # Don't break explicit words
            return

        settings = self.get_settings(word.lang)
        if settings.word_breaks_pattern is None:
            # No pattern set for this language
            return

        if (
            phonemize
            and (settings.lookup_phonemes is not None)
            and settings.lookup_phonemes(word.text)
        ):
            # Don't break apart words already in the lexicon
            return

        parts = settings.word_breaks_pattern.split(word.text)
        if len(parts) < 2:
            # Didn't split
            return

        # Preserve whitespace
        first_ws, last_ws = settings.get_whitespace(word.text_with_ws)
        last_part_idx = len(parts) - 1

        for part_idx, part_text in enumerate(parts):
            if settings.keep_whitespace:
                if part_idx == 0:
                    part_text = first_ws + part_text

                if part_idx == last_part_idx:
                    part_text += last_ws
                else:
                    part_text += settings.join_str

            yield WordNode, {
                "text": part_text.strip(),
                "text_with_ws": part_text,
                "implicit": True,
                "lang": word.lang,
            }

    def _split_punctuations(self, graph: GraphType, node: Node):
        if not isinstance(node, WordNode):
            return

        word = typing.cast(WordNode, node)
        if word.interpret_as:
            # Don't interpret words that are spoken for
            return

        settings = self.get_settings(word.lang)
        if (settings.begin_punctuations_pattern is None) and (
            settings.end_punctuations_pattern is None
        ):
            # No punctuation patterns
            return

        word_text = word.text
        first_ws, last_ws = settings.get_whitespace(word.text_with_ws)
        has_punctuation = False

        # Punctuations at the beginning of the word
        if settings.begin_punctuations_pattern is not None:
            # Split into begin punctuation and rest of word
            parts = list(
                filter(
                    None,
                    settings.begin_punctuations_pattern.split(word_text, maxsplit=1),
                )
            )

            first_word = True
            while word_text and (len(parts) == 2):
                punct_text, word_text = parts
                if first_word:
                    # Preserve leadingwhitespace
                    punct_text = first_ws + punct_text
                    first_word = False

                has_punctuation = True
                yield PunctuationWordNode, {
                    "text": punct_text.strip(),
                    "text_with_ws": punct_text,
                    "implicit": True,
                    "lang": word.lang,
                }

                parts = list(
                    filter(
                        None,
                        settings.begin_punctuations_pattern.split(
                            word_text, maxsplit=1
                        ),
                    )
                )

        # Punctuations at the end of the word
        end_punctuations: typing.List[str] = []
        if settings.end_punctuations_pattern is not None:
            # Split into rest of word and end punctuation
            parts = list(
                filter(
                    None, settings.end_punctuations_pattern.split(word_text, maxsplit=1)
                )
            )

            while word_text and (len(parts) == 2):
                word_text, punct_text = parts
                has_punctuation = True
                end_punctuations.append(punct_text)
                parts = list(
                    filter(
                        None,
                        settings.end_punctuations_pattern.split(word_text, maxsplit=1),
                    )
                )

        if not has_punctuation:
            # Leave word as-is
            return

        if settings.keep_whitespace and (not end_punctuations):
            # Preserve trailing whitespace
            word_text = word_text + last_ws

        if word_text:
            yield WordNode, {
                "text": word_text.strip(),
                "text_with_ws": word_text,
                "implicit": True,
                "lang": word.lang,
            }

        last_punct_idx = len(end_punctuations) - 1
        for punct_idx, punct_text in enumerate(reversed(end_punctuations)):
            if settings.keep_whitespace and (punct_idx == last_punct_idx):
                # Preserve trailing whitespace
                punct_text += last_ws

            yield PunctuationWordNode, {
                "text": punct_text.strip(),
                "text_with_ws": punct_text,
                "implicit": True,
                "lang": word.lang,
            }

    def _split_major_breaks(self, graph: GraphType, node: Node):
        if not isinstance(node, WordNode):
            return

        word = typing.cast(WordNode, node)
        if word.interpret_as:
            # Don't interpret words that are spoken for
            return

        settings = self.get_settings(word.lang)
        if settings.major_breaks_pattern is None:
            # No pattern set for this language
            return

        parts = settings.major_breaks_pattern.split(word.text_with_ws)
        if len(parts) < 2:
            return

        word_part = parts[0]
        break_part = parts[1]

        if word_part.strip():
            # Only yield word if there's anything but whitespace
            yield WordNode, {
                "text": word_part.strip(),
                "text_with_ws": word_part,
                "implicit": True,
                "lang": word.lang,
            }
        else:
            # Keep leading whitespace
            break_part = word_part + break_part

        yield BreakWordNode, {
            "break_type": BreakType.MAJOR,
            "text": break_part.strip(),
            "text_with_ws": break_part,
            "implicit": True,
            "lang": word.lang,
        }

    def _split_minor_breaks(self, graph: GraphType, node: Node):
        if not isinstance(node, WordNode):
            return

        word = typing.cast(WordNode, node)
        if word.interpret_as:
            # Don't interpret words that are spoken for
            return

        settings = self.get_settings(word.lang)
        if settings.minor_breaks_pattern is None:
            # No pattern set for this language
            return

        parts = settings.minor_breaks_pattern.split(word.text_with_ws)
        if len(parts) < 2:
            return

        word_part = parts[0]
        yield WordNode, {
            "text": word_part.strip(),
            "text_with_ws": word_part,
            "implicit": True,
            "lang": word.lang,
        }

        break_part = parts[1]
        yield BreakWordNode, {
            "break_type": BreakType.MINOR,
            "text": break_part.strip(),
            "text_with_ws": break_part,
            "implicit": True,
            "lang": word.lang,
        }

    def _find_parent(self, graph, node, *classes):
        """Tries to find a node whose type is in classes in the tree above node"""
        parents = []
        for parent_node in graph.predecessors(node.node):
            parent = graph.nodes[parent_node][DATA_PROP]
            if isinstance(parent, classes):
                return parent

            parents.append(parent)

        for parent in parents:
            match = self._find_parent(graph, parent, classes)
            if match is not None:
                return match

        return None

    # pylint: disable=no-self-use
    def _phonemes_for_break(
        self,
        break_type: typing.Union[str, BreakType],
        lang: typing.Optional[str] = None,
    ) -> typing.Optional[PHONEMES_TYPE]:
        if break_type == BreakType.MAJOR:
            return [IPA.BREAK_MAJOR.value]

        if break_type == BreakType.MINOR:
            return [IPA.BREAK_MINOR.value]

        return None

    # -------------------------------------------------------------------------

    def _pipeline_tokenize(
        self, graph, parent_node, text, scope_kwargs=None,
    ):
        """Splits text into word nodes"""
        if scope_kwargs is None:
            scope_kwargs = {}

        lang = self.default_lang
        if scope_kwargs is not None:
            lang = scope_kwargs.get("lang", lang)

        settings = self.get_settings(lang)
        assert settings is not None, f"No settings for {lang}"

        if settings.pre_process_text is not None:
            # Pre-process text
            text = settings.pre_process_text(text)

        # Split into separate words/separators.
        # Drop empty words (leading whitespace is still preserved).
        groups = [g for g in grouper(settings.split_pattern.split(text), 2) if g[0]]

        # Preserve whitespace.
        # NOTE: Trailing whitespace will be included in split separator.
        first_ws, _last_ws = settings.get_whitespace(text)

        for group_idx, group in enumerate(groups):
            part_str, sep_str = group
            sep_str = sep_str or ""
            word_text = part_str

            if settings.keep_whitespace:
                if group_idx == 0:
                    word_text = first_ws + word_text

                word_text += sep_str

            word_node = WordNode(
                node=len(graph),
                text=word_text.strip(),
                text_with_ws=word_text,
                implicit=True,
                **scope_kwargs,
            )
            graph.add_node(word_node.node, data=word_node)
            graph.add_edge(parent_node.node, word_node.node)

    # -------------------------------------------------------------------------
    # Pipeline Splits
    # -------------------------------------------------------------------------

    def _split_spell_out(self, graph: GraphType, node: Node):
        """Expand spell-out (a-1 -> a dash one)"""
        if not isinstance(node, WordNode):
            return

        word = typing.cast(WordNode, node)
        if word.interpret_as != InterpretAs.SPELL_OUT:
            return

        settings = self.get_settings(word.lang)

        # Preserve whitespace
        first_ws, last_ws = settings.get_whitespace(word.text_with_ws)
        last_char_idx = len(word.text) - 1

        for i, c in enumerate(word.text):
            # Look up in settings first ("." -> "dot")
            word_text = settings.spell_out_words.get(c)
            role = WordRole.DEFAULT

            if word_text is None:
                if c.isalpha():
                    # Assume this is a letter
                    word_text = c
                    role = WordRole.LETTER
                else:
                    # Leave as is (expand later in pipeline if digit, etc.)
                    word_text = c

            if not word_text:
                continue

            if settings.keep_whitespace:
                if i == 0:
                    word_text = first_ws + word_text

                if i == last_char_idx:
                    word_text += last_ws
                else:
                    word_text += settings.join_str

            yield WordNode, {
                "text": word_text.strip(),
                "text_with_ws": word_text,
                "implicit": True,
                "lang": word.lang,
                "role": role,
            }

    def _split_replacements(self, graph: GraphType, node: Node):
        """Do regex replacements on word text"""
        if not isinstance(node, WordNode):
            return

        word = typing.cast(WordNode, node)
        if word.interpret_as:
            # Don't interpret words that are spoken for
            return

        settings = self.get_settings(word.lang)

        if not settings.replacements:
            # No replacements
            return

        matched = False
        new_text = word.text_with_ws

        for pattern, template in settings.replacements:
            assert isinstance(pattern, REGEX_PATTERN)
            new_text, num_subs = pattern.subn(template, new_text)

            if num_subs > 0:
                matched = True

        if matched:
            # Tokenize new text
            for part_str, sep_str in grouper(settings.split_pattern.split(new_text), 2):
                if settings.keep_whitespace:
                    part_str += sep_str or ""

                if not part_str.strip():
                    # Ignore empty words
                    continue

                yield WordNode, {
                    "text": settings.normalize_whitespace(part_str),
                    "text_with_ws": part_str,
                    "implicit": True,
                    "lang": word.lang,
                }

    def _split_abbreviations(self, graph: GraphType, node: Node):
        """Expand abbreviations"""
        if not isinstance(node, WordNode):
            return

        word = typing.cast(WordNode, node)
        if word.interpret_as:
            # Don't interpret words that are spoken for
            return

        settings = self.get_settings(word.lang)

        if not settings.abbreviations:
            # No abbreviations
            return

        new_text: typing.Optional[str] = None
        for pattern, template in settings.abbreviations.items():
            assert isinstance(pattern, REGEX_PATTERN), pattern
            match = pattern.match(word.text_with_ws)

            if match is not None:
                new_text = match.expand(template)
                break

        if new_text is not None:
            # Tokenize new text
            for part_str, sep_str in grouper(settings.split_pattern.split(new_text), 2):
                if settings.keep_whitespace:
                    part_str += sep_str or ""

                if not part_str.strip():
                    # Ignore empty words
                    continue

                yield WordNode, {
                    "text": settings.normalize_whitespace(part_str),
                    "text_with_ws": part_str,
                    "implicit": True,
                    "lang": word.lang,
                }

    def _split_initialism(self, graph: GraphType, node: Node, phonemize: bool = True):
        """Split apart ABC or A.B.C."""
        if not isinstance(node, WordNode):
            return

        word = typing.cast(WordNode, node)
        if word.interpret_as:
            # Don't interpret words that are spoken for
            return

        settings = self.get_settings(word.lang)

        if (settings.is_initialism is None) or (settings.split_initialism is None):
            # Can't do anything without these functions
            return

        if (
            phonemize
            and (settings.lookup_phonemes is not None)
            and settings.lookup_phonemes(word.text)
        ):
            # Don't expand words already in lexicon
            return

        if not settings.is_initialism(word.text):
            # Not an initialism
            return

        # Split according to language-specific function
        parts = settings.split_initialism(word.text)
        if not parts:
            return

        # Preserve whitespace
        first_ws, last_ws = settings.get_whitespace(word.text_with_ws)
        last_part_idx = len(parts) - 1

        for part_idx, part_text in enumerate(parts):
            if not part_text:
                continue

            if settings.keep_whitespace:
                if part_idx == 0:
                    part_text = first_ws + part_text

                if part_idx == last_part_idx:
                    part_text += last_ws
                else:
                    part_text += settings.join_str

            yield WordNode, {
                "text": part_text.strip(),
                "text_with_ws": part_text,
                "implicit": True,
                "lang": word.lang,
                "role": WordRole.LETTER,
            }

    def _split_ignore_non_words(self, graph: GraphType, node: Node):
        """Mark non-words as ignored"""
        if not isinstance(node, WordNode):
            return

        word = typing.cast(WordNode, node)
        if word.interpret_as:
            # Don't interpret words that are spoken for
            return

        settings = self.get_settings(word.lang)
        if settings.is_non_word is None:
            # No function for this language
            return

        if settings.is_non_word(word.text):
            yield (IgnoreNode, {})

    # -------------------------------------------------------------------------
    # Pipeline Transformations
    # -------------------------------------------------------------------------

    def _transform_number(self, graph: GraphType, node: Node):
        if not isinstance(node, WordNode):
            return

        word = typing.cast(WordNode, node)
        if word.interpret_as and (word.interpret_as != InterpretAs.NUMBER):
            return

        settings = self.get_settings(word.lang)
        assert settings.babel_locale

        try:
            # Try to parse as a number
            # This is important to handle thousand/decimal separators correctly.
            number = babel.numbers.parse_decimal(
                word.text, locale=settings.babel_locale
            )
            word.interpret_as = InterpretAs.NUMBER
            word.number = number
        except ValueError:
            pass

    def _transform_currency(
        self, graph: GraphType, node: Node,
    ):
        if not isinstance(node, WordNode):
            return

        word = typing.cast(WordNode, node)
        if word.interpret_as and (word.interpret_as != InterpretAs.CURRENCY):
            return

        settings = self.get_settings(word.lang)

        if (settings.is_maybe_currency is not None) and not settings.is_maybe_currency(
            word.text
        ):
            # Probably not currency
            return

        assert settings.babel_locale

        # Try to parse with known currency symbols
        parsed = False
        for currency_symbol in settings.currency_symbols:
            if word.text.startswith(currency_symbol):
                num_str = word.text[len(currency_symbol) :]
                try:
                    # Try to parse as a number
                    # This is important to handle thousand/decimal separators correctly.
                    number = babel.numbers.parse_decimal(
                        num_str, locale=settings.babel_locale
                    )
                    word.interpret_as = InterpretAs.CURRENCY
                    word.currency_symbol = currency_symbol
                    word.number = number
                    parsed = True
                    break
                except ValueError:
                    pass

        # If this *must* be a currency value, use the default currency
        if (not parsed) and (word.interpret_as == InterpretAs.CURRENCY):
            default_currency = settings.default_currency
            if default_currency:
                # Forced interpretation using default currency
                try:
                    number = babel.numbers.parse_decimal(
                        word.text, locale=settings.babel_locale
                    )
                    word.interpret_as = InterpretAs.CURRENCY
                    word.currency_name = default_currency
                    word.number = number
                except ValueError:
                    pass

    def _transform_date(self, graph: GraphType, node: Node):
        if not isinstance(node, WordNode):
            return

        word = typing.cast(WordNode, node)
        if word.interpret_as and (word.interpret_as != InterpretAs.DATE):
            return

        settings = self.get_settings(word.lang)

        if (settings.is_maybe_date is not None) and not settings.is_maybe_date(
            word.text
        ):
            # Probably not a date
            return

        assert settings.dateparser_lang

        dateparser_kwargs: typing.Dict[str, typing.Any] = {
            "settings": {"STRICT_PARSING": True},
            "languages": [settings.dateparser_lang],
        }

        date = dateparser.parse(word.text, **dateparser_kwargs)
        if date is not None:
            word.interpret_as = InterpretAs.DATE
            word.date = date
        elif word.interpret_as == InterpretAs.DATE:
            # Try again without strict parsing
            dateparser_kwargs["settings"]["STRICT_PARSING"] = False
            date = dateparser.parse(word.text, **dateparser_kwargs)
            if date is not None:
                word.date = date

    # -------------------------------------------------------------------------
    # Verbalization
    # -------------------------------------------------------------------------

    def _verbalize_number(self, graph: GraphType, node: Node):
        """Split numbers into words"""
        if not isinstance(node, WordNode):
            return

        word = typing.cast(WordNode, node)
        if (word.interpret_as != InterpretAs.NUMBER) or (word.number is None):
            return

        settings = self.get_settings(word.lang)

        if (settings.is_maybe_number is not None) and not settings.is_maybe_number(
            word.text
        ):
            # Probably not a number
            return

        assert settings.num2words_lang
        num2words_kwargs = {"lang": settings.num2words_lang}
        decimal_nums = [word.number]

        if word.format == InterpretAsFormat.NUMBER_CARDINAL:
            num2words_kwargs["to"] = "cardinal"
        elif word.format == InterpretAsFormat.NUMBER_ORDINAL:
            num2words_kwargs["to"] = "ordinal"
        elif word.format == InterpretAsFormat.NUMBER_YEAR:
            num2words_kwargs["to"] = "year"
        elif word.format == InterpretAsFormat.NUMBER_DIGITS:
            num2words_kwargs["to"] = "cardinal"
            decimal_nums = [Decimal(d) for d in str(word.number.to_integral_value())]

        for decimal_num in decimal_nums:
            num_has_frac = (decimal_num % 1) != 0

            # num2words uses the number as an index sometimes, so it *has* to be
            # an integer, unless we're doing currency.
            if num_has_frac:
                final_num = float(decimal_num)
            else:
                final_num = int(decimal_num)

            # Convert to words (e.g., 100 -> one hundred)
            num_str = num2words(final_num, **num2words_kwargs)

            # Remove all non-word characters
            num_str = re.sub(r"\W", settings.join_str, num_str).strip()

            # Split into separate words/separators
            groups = list(grouper(settings.split_pattern.split(num_str), 2))

            # Preserve whitespace
            first_ws, last_ws = settings.get_whitespace(word.text_with_ws)
            last_group_idx = len(groups) - 1

            # Split into separate words/separators
            for group_idx, group in enumerate(groups):
                part_str, sep_str = group
                if not part_str:
                    continue

                sep_str = sep_str or ""
                number_word_text = part_str

                if settings.keep_whitespace:
                    if group_idx == 0:
                        number_word_text = first_ws + number_word_text

                    if group_idx == last_group_idx:
                        number_word_text += last_ws
                    else:
                        number_word_text += sep_str

                number_word = WordNode(
                    node=len(graph),
                    implicit=True,
                    lang=word.lang,
                    text=number_word_text.strip(),
                    text_with_ws=number_word_text,
                )
                graph.add_node(number_word.node, data=number_word)
                graph.add_edge(word.node, number_word.node)

    def _verbalize_date(self, graph: GraphType, node: Node):
        """Split dates into words"""
        if not isinstance(node, WordNode):
            return

        word = typing.cast(WordNode, node)
        if (word.interpret_as != InterpretAs.DATE) or (word.date is None):
            return

        settings = self.get_settings(word.lang)
        assert settings.babel_locale
        assert settings.num2words_lang

        date = word.date
        date_format = (word.format or settings.default_date_format).strip().upper()
        day_card_str = ""
        day_ord_str = ""
        month_str = ""
        year_str = ""

        if "M" in date_format:
            month_str = babel.dates.format_date(
                date, "MMMM", locale=settings.babel_locale
            )

        num2words_kwargs = {"lang": settings.num2words_lang}

        if "D" in date_format:
            # Cardinal day (1 -> one)
            num2words_kwargs["to"] = "cardinal"
            day_card_str = num2words(date.day, **num2words_kwargs)

        if "O" in date_format:
            # Ordinal day (1 -> first)
            num2words_kwargs["to"] = "ordinal"
            day_ord_str = num2words(date.day, **num2words_kwargs)

        if "Y" in date_format:
            num2words_kwargs["to"] = "year"
            year_str = num2words(date.year, **num2words_kwargs)

        # Transform into Python format string
        # MDY -> {M} {D} {Y}
        date_format_str = settings.join_str.join(f"{{{c}}}" for c in date_format)
        date_str = date_format_str.format(
            **{"M": month_str, "D": day_card_str, "O": day_ord_str, "Y": year_str}
        )

        # Split into separate words/separators
        groups = list(grouper(settings.split_pattern.split(date_str), 2))

        # Preserve whitespace
        first_ws, last_ws = settings.get_whitespace(word.text_with_ws)
        last_group_idx = len(groups) - 1

        for group_idx, group in enumerate(groups):
            part_str, sep_str = group
            if not part_str:
                continue

            sep_str = sep_str or ""
            date_word_text = part_str

            if settings.keep_whitespace:
                if group_idx == 0:
                    date_word_text = first_ws + date_word_text

                if group_idx == last_group_idx:
                    date_word_text += last_ws
                else:
                    date_word_text += sep_str

            if not date_word_text:
                continue

            date_word = WordNode(
                node=len(graph),
                implicit=True,
                lang=word.lang,
                text=date_word_text.strip(),
                text_with_ws=date_word_text,
            )
            graph.add_node(date_word.node, data=date_word)
            graph.add_edge(word.node, date_word.node)

    def _verbalize_currency(
        self, graph: GraphType, node: Node,
    ):
        """Split currency amounts into words"""
        if not isinstance(node, WordNode):
            return

        word = typing.cast(WordNode, node)
        if (
            (word.interpret_as != InterpretAs.CURRENCY)
            or ((word.currency_symbol is None) and (word.currency_name is None))
            or (word.number is None)
        ):
            return

        settings = self.get_settings(word.lang)
        assert settings.num2words_lang

        decimal_num = word.number

        # True if number has non-zero fractional part
        num_has_frac = (decimal_num % 1) != 0

        num2words_kwargs = {"lang": settings.num2words_lang, "to": "currency"}

        # Name of currency (e.g., USD)
        if not word.currency_name:
            currency_name = settings.default_currency
            if settings.currencies:
                # Look up currency in locale
                currency_name = settings.currencies.get(
                    word.currency_symbol or "", settings.default_currency
                )

            word.currency_name = currency_name

        num2words_kwargs["currency"] = word.currency_name

        # Custom separator so we can remove 'zero cents'
        num2words_kwargs["separator"] = "|"

        try:
            num_str = num2words(float(decimal_num), **num2words_kwargs)
        except Exception:
            _LOGGER.exception("verbalize_currency: %s", word)
            return

        # Post-process currency words
        if num_has_frac:
            # Discard num2words separator
            num_str = num_str.replace("|", "")
        else:
            # Remove 'zero cents' part
            num_str = num_str.split("|", maxsplit=1)[0]

        # Remove all non-word characters
        num_str = re.sub(r"\W", settings.join_str, num_str).strip()

        # Split into separate words/separators
        groups = list(grouper(settings.split_pattern.split(num_str), 2))

        # Preserve whitespace
        first_ws, last_ws = settings.get_whitespace(word.text_with_ws)
        last_group_idx = len(groups) - 1

        # Split into separate words
        for group_idx, group in enumerate(groups):
            part_str, sep_str = group
            if not part_str:
                continue

            sep_str = sep_str or ""
            currency_word_text = part_str

            if settings.keep_whitespace:
                if group_idx == 0:
                    currency_word_text = first_ws + currency_word_text

                if group_idx == last_group_idx:
                    currency_word_text += last_ws
                else:
                    currency_word_text += sep_str

            currency_word = WordNode(
                node=len(graph),
                implicit=True,
                lang=word.lang,
                text=currency_word_text.strip(),
                text_with_ws=currency_word_text,
            )
            graph.add_node(currency_word.node, data=currency_word)
            graph.add_edge(word.node, currency_word.node)