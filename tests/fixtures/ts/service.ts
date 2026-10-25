import { Base } from "./base";

/** Fetch a user by id. */
export async function fetchUser(id: string): Promise<User> {
  return api.get(id);
}

export const parseConfig = (raw: string) => {
  return JSON.parse(raw);
};

export class UserService extends Base {
  private cache = new Map();

  onChange = (e: Event) => {
    this.cache.clear();
  };

  constructor(private api: Api) {
    super();
  }

  async getUser(id: string): Promise<User> {
    return this.api.get(id);
  }
}

export interface User {
  id: string;
}

export type UserId = string;
