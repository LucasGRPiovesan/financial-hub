import type { enUS } from "./en-US";

/** The CLDR plural categories a message may spell out; `other` is mandatory (it is the fallback). */
export interface PluralForms {
  readonly other: string;
  readonly zero?: string;
  readonly one?: string;
  readonly two?: string;
  readonly few?: string;
  readonly many?: string;
}

/** `as const` narrows every string to its literal; the shape other locales must follow widens them back. */
type Widen<T> = T extends string ? string : T extends { readonly other: string } ? PluralForms : { readonly [K in keyof T]: Widen<T[K]> };

/** The shape of a dictionary. pt-BR is declared as this type, so a key missing from it fails `tsc`. */
export type Messages = Widen<typeof enUS>;

type Paths<T, Prefix extends string = ""> = {
  [K in keyof T & string]: T[K] extends string
    ? `${Prefix}${K}`
    : T[K] extends { readonly other: string }
      ? `${Prefix}${K}`
      : Paths<T[K], `${Prefix}${K}.`>;
}[keyof T & string];

/** Every translatable leaf, as a dot path: "members.added". */
export type MessageKey = Paths<typeof enUS>;

export type MessageParams = Readonly<Record<string, string | number>>;

export type Translate = (key: MessageKey, params?: MessageParams) => string;
